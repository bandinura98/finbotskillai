#!/usr/bin/env python3
"""Order tickets, the manual order slip (emir fisi) and the decision log for
trader-broker (Borsa Istanbul). Standard library only.

This tool never talks to a broker, never places anything and never sends
anything anywhere. Most Turkish brokers have no public order API, so YOU type
each approved order into your own broker app. The tool does its work around
the one moment that matters, your evet:

  show      renders each proposed order as a plain ticket plus an emir fisi:
            Hisse, Yon (AL/SAT), Lot, Emir tipi (Limit/Piyasa), Fiyat,
            Zarar-durdur (stop), Kar-al (target), Sure (Gun) and the
            estimated costs. An order is READY only if trader-risk passed
            THIS exact order today (matched by fingerprint) and nobody has
            decided on it yet.
  approve   records your explicit evet for ONE order, with your reply word for
            word, and prints the emir fisi to type into your app. It refuses
            "hepsine evet", "tamam", "olur", "ok", a yes that names a
            different stock or amount, and any order that is not READY.
  decline   records a no (hayir).
  result    records what happened after you typed it into your broker app:
            placed (with the app's order number), denied (you did not enter
            it after all) or failed (the app rejected it).
  fill      records a fill in trader/portfolio.json (cash, shares, costs).
  flatten   builds a sell ticket that closes a symbol's open position by
            flat_by. It still goes through trader-risk and your evet.
  log       prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "evet"
    python3 ticket.py result --index 1 --status placed --broker-id 123456
    python3 ticket.py fill --symbol THYAO --side buy --qty 181 --price 312.50
    python3 ticket.py flatten --symbol THYAO --qty 181
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are
Istanbul dates (TSI); --now "2026-11-09 10:30" pins the clock.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

# The order fields that identify an order (same tuple in trader-risk/check.py: the fingerprint).
ORDER_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                "stop_loss_limit_price", "advanced_instructions")
STRING_FIELDS = ("qty", "notional", "limit_price", "stop_price", "trail_price", "trail_percent",
                 "take_profit_limit_price", "stop_loss_stop_price", "stop_loss_limit_price")
TIF_WORDS = {"day": "good for today", "gtc": "good until canceled", "ioc": "immediate or cancel",
             "fok": "fill or kill"}
TIF_TR = {"day": "Gün", "gtc": "İptale kadar geçerli (İKG)", "ioc": "Kalanı iptal et (KİE)",
          "fok": "Gerçekleşmezse iptal et (GİE)"}
TYPE_TR = {"market": "Piyasa", "limit": "Limit", "stop": "Şartlı (stop)", "stop_limit": "Şartlı limit"}
YES_WORDS = ("evet", "yes")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each",
               "hepsi", "hepsine", "hepsini", "hepsinin", "hep", "tümü", "tümüne", "tümünü", "tamamı",
               "tamamına", "ikisi", "ikisine", "ikisini", "ikisinde", "kalan", "kalanı", "kalanlar",
               "kalanlara", "toplu", "her"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if", "hayır", "hayir", "değil", "degil", "ama", "fakat", "ancak",
               "bekle", "dur", "iptal", "belki", "sonra", "eğer", "eger", "olmaz", "yok", "vazgeç",
               "vazgec", "galiba", "sanırım"}
DEFAULT_COSTS = {
    "commission_rate": 0.0015,   # broker commission per side, fraction of notional (binde 1,5); set yours
    "bsmv_rate": 0.05,           # BSMV on the commission
    "exchange_fee_rate": 0.00003,  # borsa payı per side (approx.)
}


class TicketError(Exception):
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
        raise TicketError("--now wants a time like \"2026-11-09 10:30\" (Istanbul, TSİ) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def ny_iso(now_utc):
    """naive UTC -> "2026-11-09T10:30:00+03:00" (Istanbul wall clock with its offset; name kept)."""
    off = eastern_offset(now_utc)
    return "%s%+03d:00" % (to_eastern(now_utc).replace(microsecond=0).isoformat(), off)


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as trader-risk/check.py: identifies the exact order."""
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _str_num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return str(v)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def order_fields(order):
    """The order's own fields (numbers as strings). "why" and "plan" are notes, not part of the order."""
    out = {}
    for k in ORDER_FIELDS:
        if k in order and order[k] is not None:
            out[k] = _str_num(order[k]) if k in STRING_FIELDS else order[k]
    return out


def cost_rates(cfg):
    out = dict(DEFAULT_COSTS)
    for k in DEFAULT_COSTS:
        v = _f((cfg or {}).get(k))
        if v is not None and v >= 0:
            out[k] = v
    return out


def side_cost(notional, c):
    com = notional * c["commission_rate"]
    return com * (1 + c["bsmv_rate"]) + notional * c["exchange_fee_rate"]


def _money(v):
    """1234.5 -> '1.234,50 TL' (Turkish grouping)."""
    s = "{:,.2f}".format(float(v))
    return s.replace(",", "_").replace(".", ",").replace("_", ".") + " TL"


money = _money


def _p(v):
    return "%.2f" % float(v)


def _tr(v):
    """312.5 -> "312,50": a price the way a Turkish broker app shows it."""
    return ("%.2f" % float(v)).replace(".", ",")


def is_bracket(order):
    oc = str(order.get("order_class") or "").lower()
    legs = order.get("take_profit_limit_price") is not None or order.get("stop_loss_stop_price") is not None
    return oc == "bracket" or (not oc and legs)


def entry_price(order):
    """The price one share is expected to cost: the limit price, else the plan's entry."""
    if str(order.get("type", "market")) in ("limit", "stop_limit") and _f(order.get("limit_price")):
        return _f(order["limit_price"])
    return _f((order.get("plan") or {}).get("entry"))


def max_loss(order, cfg=None):
    """(lira, stop) lost if the stop fills exactly at the stop, round-trip costs included, or (None, stop)."""
    stop = _f(order.get("stop_loss_stop_price")) or _f((order.get("plan") or {}).get("stop"))
    q = _f(order.get("qty"))
    if not stop or not q:
        return None, stop
    plan = order.get("plan") or {}
    if str(order.get("type", "market")) == "limit" and _f(order.get("limit_price")):
        entry = _f(order["limit_price"])
    elif _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = int(round(abs(entry - stop) * 100)) / 100.0
    c = cost_rates(cfg)
    cps = side_cost(entry, c) + side_cost(stop, c)
    return round(q * (risk + cps), 2), stop


def estimated_costs(order, cfg=None):
    """{"notional", "this_side", "round_trip"} in TL, or None without a price. Round trip = this
    order plus selling at the stop (a bracket) or nothing more (a plain order)."""
    q = _f(order.get("qty"))
    px = entry_price(order)
    if order.get("notional") is not None:
        notional = _f(order["notional"])
    elif q and px:
        notional = q * px
    else:
        return None
    c = cost_rates(cfg)
    this = side_cost(notional, c)
    out = {"notional": round(notional, 2), "this_side": round(this, 2)}
    stop = _f(order.get("stop_loss_stop_price"))
    if q and stop and is_bracket(order):
        out["round_trip"] = round(this + side_cost(q * stop, c), 2)
    return out


def plain_parts(order, cfg=None):
    """The ticket in plain words, one part per line."""
    side = str(order.get("side", "")).upper()
    sym = order.get("symbol", "?")
    typ = str(order.get("type", "market"))
    tif = TIF_WORDS.get(str(order.get("time_in_force", "day")), order.get("time_in_force"))
    if is_bracket(order):
        how = "limit %s" % _p(order["limit_price"]) if typ == "limit" and order.get("limit_price") else typ
        parts = ["%s %s %s, %s, %s" % (side, _str_num(order.get("qty")), sym, how, tif)]
        if order.get("stop_loss_stop_price") is not None:
            leg = "stop-loss %s" % _p(order["stop_loss_stop_price"])
            if order.get("stop_loss_limit_price") is not None:
                leg += " (limit %s)" % _p(order["stop_loss_limit_price"])
            parts.append(leg)
        if order.get("take_profit_limit_price") is not None:
            parts.append("take-profit %s" % _p(order["take_profit_limit_price"]))
        loss, stop = max_loss(order, cfg)
        if loss is not None:
            parts.append("max loss about %s with costs if the stop fills at %s (it can fill worse in a fast market)"
                         % (_money(loss), _p(stop)))
        return parts
    if order.get("notional") is not None:
        what = "%s of %s" % (_money(order["notional"]), sym)
    else:
        q = _str_num(order.get("qty"))
        what = "%s share%s of %s" % (q, "" if q == "1" else "s", sym)
    if typ == "limit":
        how = "limit order at %s" % _p(order.get("limit_price"))
    elif typ == "stop":
        how = "stop order at %s" % _p(order.get("stop_price"))
    elif typ == "stop_limit":
        how = "stop %s, limit %s" % (_p(order.get("stop_price")), _p(order.get("limit_price")))
    else:
        how = "%s order" % typ.replace("_", " ")
    return ["%s %s, %s, %s" % (side, what, how, tif)]


def plain(order, cfg=None):
    """BUY 181 THYAO, market, good for today / stop-loss 308.00 / take-profit 317.00 / max loss about ..."""
    return " / ".join(plain_parts(order, cfg))


def slip(order, cfg=None):
    """The manual order slip (emir fisi) as an ordered list of (label, value)."""
    side = str(order.get("side", "")).lower()
    typ = str(order.get("type", "market")).lower()
    rows = [("Hisse", str(order.get("symbol", "?")).upper()),
            ("Yön", "AL" if side == "buy" else "SAT" if side == "sell" else side.upper())]
    if order.get("notional") is not None:
        rows.append(("Tutar", _money(order["notional"]) + " (tutar emri; uygulaman lot istiyorsa lota çevir)"))
    else:
        rows.append(("Lot", _str_num(order.get("qty"))))
    rows.append(("Emir tipi", TYPE_TR.get(typ, typ)))
    if typ in ("limit", "stop_limit") and order.get("limit_price") is not None:
        rows.append(("Fiyat", _tr(order["limit_price"])))
    elif typ == "stop" and order.get("stop_price") is not None:
        rows.append(("Fiyat", "tetik " + _tr(order["stop_price"])))
    else:
        rows.append(("Fiyat", "piyasa (o anki en iyi fiyat)"))
    if order.get("stop_loss_stop_price") is not None:
        rows.append(("Zarar-durdur", _tr(order["stop_loss_stop_price"]) + " (stop: fiyat buna inerse SAT)"))
    if order.get("take_profit_limit_price") is not None:
        rows.append(("Kâr-al", _tr(order["take_profit_limit_price"]) + " (hedef: fiyat buna çıkarsa SAT)"))
    rows.append(("Süre", TIF_TR.get(str(order.get("time_in_force", "day")).lower(),
                                     order.get("time_in_force"))))
    est = estimated_costs(order, cfg)
    if est:
        rows.append(("Tahmini tutar", "~" + _money(est["notional"])))
        cost = "~%s bu emir (komisyon + BSMV + borsa payı)" % _money(est["this_side"])
        if "round_trip" in est:
            cost += ", ~%s gidiş-dönüş (stop'ta satışla)" % _money(est["round_trip"])
        rows.append(("Tahmini maliyet", cost))
    return rows


def slip_lines(order, cfg=None, indent="  "):
    rows = slip(order, cfg)
    w = max(len(k) for k, _ in rows)
    out = [indent + "EMİR FİŞİ (bunu kendi aracı kurum uygulamana SEN gir; Claude hiçbir yere göndermez)"]
    for k, v in rows:
        out.append(indent + "  %s : %s" % (k.ljust(w), v))
    if is_bracket(order):
        out.append(indent + "  Not: çoğu uygulamada bu üçü tek emir değildir. Önce AL emrini gir; gerçekleşince")
        out.append(indent + "  zarar-durdur ve kâr-al için şartlı SAT emirlerini gir (uygulaman destekliyorsa).")
        out.append(indent + "  Desteklemiyorsa trader-alerts alarmlarıyla takip et ve elle sat.")
    return out


def _numbers(raw):
    """Numbers in a reply, each as a set of readings: "312,50" -> {312.5}, "1.000" -> {1000.0, 1.0}."""
    out = []
    for tok in re.findall(r"\d+(?:[.,]\d+)*", raw):
        cands = set()
        if "," in tok and "." in tok:
            cands.add(_f(tok.replace(".", "").replace(",", ".")))     # 1.234,50
            cands.add(_f(tok.replace(",", "")))                       # 1,234.50
        elif "," in tok:
            cands.add(_f(tok.replace(",", ".")) if tok.count(",") == 1 else None)
            cands.add(_f(tok.replace(",", "")))
        elif tok.count(".") > 1:
            cands.add(_f(tok.replace(".", "")))
        else:
            cands.add(_f(tok))
            if re.match(r"^\d{1,3}\.\d{3}$", tok):
                cands.add(_f(tok.replace(".", "")))
        cands.discard(None)
        if cands:
            out.append((tok, cands))
    return out


def amount_numbers(order, cfg=None):
    nums = []
    for k in ("notional", "qty", "limit_price", "stop_loss_stop_price", "take_profit_limit_price",
              "stop_loss_limit_price"):
        if order.get(k) is not None:
            v = _f(order[k])
            if v is not None:
                nums.append(v)
    loss, _ = max_loss(order, cfg)
    if loss is not None:
        nums.append(loss)
    return nums


def _lower_tr(s):
    return s.replace("İ", "i").replace("I", "ı").lower()


UNIVERSE_FILES = ("trader/universe.txt",
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "trader-scanner",
                               "bist_universe.txt"))
CAPS_OK = {"EVET", "YES", "LOT", "LOTU", "SAT", "TL", "TRY", "OK", "TSİ", "TSI", "AL", "BIST"}


def known_codes(paths=UNIVERSE_FILES):
    """BIST codes from the share lists (CODE|Name a line, # comments), if they exist. Never fails."""
    out = set()
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    code = line.split("#", 1)[0].split("|", 1)[0].strip().upper()
                    if re.match(r"^[A-Z][A-Z0-9]{2,5}$", code):
                        out.add(code)
        except (OSError, UnicodeDecodeError):
            continue
    return out


def named_stocks(raw, sym, batch_symbols=()):
    """Other stocks a reply names: a batch symbol or a known BIST code (any case), or a code-like word the
    user typed in capitals in an otherwise mixed-case reply ("evet ASELS")."""
    words = set(re.findall(r"[A-Z0-9]+", raw.upper()))
    others = ({str(s).upper() for s in batch_symbols} | known_codes()) - {sym}
    named = words & others
    if raw != raw.upper():
        named |= {w for w in re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{2,5}(?![A-Za-z0-9])", raw)
                  if w != sym and w not in CAPS_OK}
    return named


def parse_yes(reply, order, batch_symbols=(), cfg=None):
    """Is this reply an explicit evet for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not an evet"
    low = _lower_tr(raw)
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", low)
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"evet\" counts (not tamam, olur, ok, sure, go)"
    if BATCH_WORDS & set(words):
        return False, "an evet covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("symbol", "")).upper()
    named = named_stocks(raw, sym, batch_symbols)
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = _numbers(raw)
    if nums:
        want = amount_numbers(order, cfg)
        if not any(abs(n - w) < 0.005 for _, cands in nums for n in cands for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(t for t, _ in nums), plain(order, cfg))
    return True, "explicit evet for %s" % plain(order, cfg)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, ("APPROVED by you at %s, waiting for you to type the emir fisi into your broker app "
                           "(then ticket.py result)" % rec.get("at")), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your evet", rv


def kind_of(order, rv):
    """entry or exit, as trader-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


def broker_name(cfg):
    b = str((cfg or {}).get("broker") or "").strip()
    return b if b and b.lower() not in ("manual", "manuel", "alpaca", "ibkr") else "your broker app"


def account_label(cfg):
    if (cfg or {}).get("paper", True):
        return "KAĞIT ÜZERİNDE (sanal), do not type it into a real account"
    return "GERÇEK HESAP, REAL MONEY"


# ---------------------------------------------------------------- rendering

def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    lines = ["ORDER %d of %d, %s, manual entry in %s" % (idx, total, account_label(cfg), broker_name(cfg))]
    for part in plain_parts(order, cfg):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    lines.extend(slip_lines(order, cfg))
    if cfg.get("paper", True):
        lines.append("  Kağıt üzerinde / sanal: \"paper\" is true, so track this in a demo account or a notebook,")
        lines.append("  not in your real account.")
    if ready:
        lines.append("  Reply \"evet\" to approve this one order, or \"hayır\" to skip it. Nothing is sent")
        lines.append("  anywhere: after your evet you type the emir fisi into %s yourself." % broker_name(cfg))
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    loss, stop = max_loss(order, cfg)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order, cfg),
            "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)),
            "broker": broker_name(cfg), "market": "BIST", "currency": "TRY",
            "order": order_fields(order), "emir_fisi": dict(slip(order, cfg)),
            "estimated_costs": estimated_costs(order, cfg), "why": order.get("why", ""),
            "bracket": is_bracket(order), "max_loss": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, fee=None):
    """Record a filled order in trader/portfolio.json: cash and shares, with a
    cost-basis lot per buy (oldest lots are relieved first on a sell). The
    commission + BSMV + borsa payi (fee, TL; estimated from the config when not
    given) comes off the cash. Returns (portfolio, fee)."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = symbol.upper()
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    if abs(qty - round(qty)) > 1e-9:
        raise TicketError("BIST trades whole shares (1 lot = 1 share), got qty %s" % _str_num(qty))
    cost = round(qty * price, 2)
    fee = round(float(fee), 2) if fee is not None else round(side_cost(cost, cost_rates(cfg)), 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty,
                     "cost": round(cost + fee, 2)})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pf["cash"] = round(float(pf.get("cash", 0)) - cost - fee, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (_str_num(qty), symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost - fee, 2)
    pf.setdefault("currency", "TRY")
    write_json(portfolio_path, pf)
    return pf, fee


def log_fill(state_path, now, today, symbol, side, qty, price, fee=None):
    """A fill in the log, so trader-risk can count trades (a buy and a sell the same day)."""
    state = load_state(state_path)
    rec = {"at": ny_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
           "qty": _str_num(float(qty)), "price": float(price),
           "plain": "%s %s %s at %s" % (side.upper(), _str_num(float(qty)), symbol.upper(), _p(price))}
    if fee is not None:
        rec["fee"] = fee
    state.setdefault("orders", []).append(rec)
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or to_eastern(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-11-09")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: an evet covers one ticket, never the batch. Claude never places orders. "
          "Education and tooling, not investment advice (yatırım tavsiyesi değildir).")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders], cfg)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(order, cfg)
    rec = {"at": ny_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "qty": order.get("qty"), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "plain": plain(order, cfg), "status": "approved", "reply": a.reply,
           "paper": bool(cfg.get("paper", True)), "broker": broker_name(cfg), "market": "BIST"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your evet for order %d: %s." % (a.index, plain(order, cfg)))
    print("Nothing was sent anywhere. Type this into %s yourself:" % broker_name(cfg))
    print("\n".join(slip_lines(order, cfg)))
    if cfg.get("paper", True):
        print("Kağıt üzerinde / sanal (\"paper\": true): use a demo account or a notebook, not real money.")
    print("Then record what happened: ticket.py result --index %d --status placed|denied|failed "
          "[--broker-id <emir no>] [--filled-qty Q --filled-price P]" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ny_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "plain": plain(order, cfg), "status": "declined", "reply": a.reply or "hayır"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order, cfg)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no evet recorded today; a result can only follow an approval" % a.index)
    new = {"at": ny_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        _, fee = apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today)
        log_fill(a.state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, fee)
        print("Added the fill to %s (estimated costs %s)." % (a.portfolio, _money(fee)))
    elif a.status == "placed":
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (rec["symbol"], rec["side"]))
        if rec.get("kind") == "entry" and is_bracket(order):
            print("Then enter the zarar-durdur and kâr-al sells in your app; when one fills, record that sell "
                  "the same way.")
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or to_eastern(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-11-09")
    fee = _f(a.fee) if a.fee is not None else None
    pf, fee = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date, fee)
    log_fill(a.state, now, date, a.symbol, a.side, a.qty, a.price, fee)
    print("Recorded %s %s %s at %s on %s in %s (costs %s). Cash now %s."
          % (a.side, a.qty, a.symbol.upper(), _p(a.price), date, a.portfolio, _money(fee), _money(pf["cash"])))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    sym = a.symbol.strip().upper()
    if sym.endswith(".IS"):
        sym = sym[:-3]
    now, today = _clock(a)
    source = "your broker app"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0:
            raise TicketError("--qty wants the number of shares (lots) you hold, like --qty 181")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if str(p.get("ticker") or p.get("symbol") or "").upper() == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check your broker app and pass --qty "
                              "if you do hold some" % (sym, a.portfolio))
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == sym else None) \
        or cfg.get("flat_by") or "17:50"
    order = {"symbol": sym, "side": "sell", "qty": _str_num(qty), "type": "market", "time_in_force": "day",
             "why": "flat by %s TSİ: closes the %s day trade before the close (qty from %s)"
             % (flat_by, sym, source)}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "market": "BIST", "currency": "TRY", "paper": bool(cfg.get("paper", True)),
           "note": "Flatten ticket. Nothing is sent anywhere: trader-risk checks it, you say evet in the chat, "
                   "and you type the emir fisi into your own broker app.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s TSİ)." % (sym, plain(order, cfg), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against the position in your broker app before you say evet."
              % source)
    print("Next:")
    print("  1. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's evet for this one order.")
    print("  3. If your zarar-durdur / kâr-al sell orders are still waiting in your app, they hold these lots")
    print("     and a new sell can be rejected. Cancel them in the app first, then enter the emir fisi.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" emir no " + str(r["broker_order_id"])) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % _money(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets, emir fisi and your evet (trader-broker). "
                                             "Never places or sends orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this Istanbul time (TSİ), e.g. \"2026-11-09 10:30\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in Istanbul)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets and their emir fisi")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit evet for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened after you typed the emir fisi into your app")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"],
                   help="placed: entered and accepted; denied: you did not enter it; failed: the app rejected it")
    p.add_argument("--broker-id", help="the order number your broker app shows (emir no)")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--fee", help="commission + BSMV + borsa payi in TL (default: estimated from the config)")
    p.add_argument("--date", help="YYYY-MM-DD (default: today in Istanbul)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="a sell ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares (lots) you hold, from your broker app (default: trader/portfolio.json)")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):      # TSİ and Turkish text on a non-UTF-8 pipe
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
