#!/usr/bin/env python3
"""Earnings reader for trader-analyst (Borsa Istanbul). Standard library only.

Pulls a BIST company's income statement from İş Yatırım's public MaliTablo
JSON endpoint and lays out what changed, quarter by quarter: revenue, gross,
operating and net margins, net income (the parent's share when given),
EPS when the statement carries it, and growth against the same quarter a
year earlier.

    python3 facts.py THYAO
    python3 facts.py THYAO ASELS --quarters 8
    python3 facts.py GARAN --group UFRS            # banks and other financials
    python3 facts.py THYAO --json
    python3 facts.py THYAO ASELS GARAN --metrics trader/metrics.json
    python3 facts.py THYAO --save thyao.json       # keep the raw answers ...
    python3 facts.py THYAO --offline thyao.json    # ... and read them without network

Source (UNOFFICIAL: a public data endpoint behind İş Yatırım's website, not an
API anyone promises to keep; no key, no documentation, it can change or
disappear at any time):
  https://www.isyatirim.com.tr/_layouts/15/IsYatirim.Website/Common/Data.aspx/MaliTablo
      ?companyCode=THYAO&exchange=TRY&financialGroup=XI_29
      &year1=2025&period1=9&year2=2025&period2=6&year3=2025&period3=3&year4=2024&period4=12
It answers {"ok": true, "errorCode", "errorDescription", "transactionId",
"value": [{"itemCode", "itemDescTr", "itemDescEng", "value1".."value4"}, ...]}
(checked 2026-11-09): every line of the balance sheet (itemCode 1..., 2...),
the income statement (3...) and the cash flow (4...), values as strings of
whole lira ("690825000000") or null, one column per requested period in the
order asked, at most four periods a request, so the tool makes several
requests (a pause between them). financialGroup XI_29 is the
layout for industrial companies, UFRS for banks and financials: the tool
tries XI_29 and falls back to UFRS when the answer is empty (a bank asked for
XI_29 answers {"ok": true, "value": []}; --group forces one).
If the answer's shape changes the tool stops with a plain message.
The official source of every number is the company's financial report on KAP:
https://www.kap.org.tr/tr/bildirim-sorgu

How the quarters are built:
  - Turkish statements are year to date: the June column is the first half,
    September nine months, December the full year. Q1 is the March figure;
    Q2, Q3 and Q4 are each cumulative figure minus the one before it (marked
    "derived"). A quarter whose earlier cumulative figure is missing is left out,
    and a column with no numbers at all (a period not reported yet) is ignored.
  - Statements under inflation accounting (TMS 29) state each cumulative
    figure in the lira of its own period end, so a derived quarter mixes
    purchasing power: read it as an approximation and check KAP.
  - Line items are found by their Turkish or English description: Hasılat /
    Satış Gelirleri (revenue), Brüt Kâr (gross profit), Esas Faaliyet Kârı /
    Faaliyet Kârı (operating profit), Ana Ortaklık Payları (net income
    attributable to the parent; else Dönem Kârı). EPS is not read: İş Yatırım
    rounds it to whole lira. Banks (UFRS) have no revenue line: their net income
    (Net Dönem Kârı, Grubun Kârı) is mapped and the top income-statement lines
    are printed raw; when nothing maps only the raw lines are printed.
  - Companies with a fiscal year that does not end in December are not handled
    specially: check their quarters against KAP.

Values are Turkish lira as İş Yatırım gives them; the table shows millions (mn TL).
--metrics FILE merges one row per company into a JSON file (TTM revenue and
net income, margins, growth). Offline: --offline FILE reads a saved answer or
a bundle written by --save (see load_offline for the accepted shapes).

Numbers only: this tool never says whether a share is a buy
(yatırım tavsiyesi değildir).
"""

import argparse
import datetime as _dt
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MALI_URL = ("https://www.isyatirim.com.tr/_layouts/15/IsYatirim.Website/Common/Data.aspx/MaliTablo"
            "?companyCode=%s&exchange=TRY&financialGroup=%s%s")
SOURCE = "İş Yatırım MaliTablo (unofficial endpoint)"
KAP_URL = "https://www.kap.org.tr/tr/bildirim-sorgu"
BROWSER_UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
GROUPS = ("XI_29", "UFRS")
TR_OFFSET = 3  # Europe/Istanbul, UTC+3 all year since 2016, no DST
SHAPE_HELP = ("İş Yatırım's MaliTablo answer no longer has the expected shape "
              "({\"value\": [{\"itemDescTr\", \"value1\"...}]}). The endpoint is unofficial and may "
              "have changed. Read the financial report on KAP instead: " + KAP_URL)

# Patterns run on fold()ed descriptions (lower case, Turkish letters -> ASCII,
# a leading "XXIII. " / "23.1 " numbering stripped). Each key: Turkish patterns
# first, then English; within a key the first pattern that matches any line
# wins, so the order is the preference. Only income-statement lines (itemCode
# 3...) are searched: the answer also carries the balance sheet, whose equity
# section has a "Dönem Net Kar/Zararı" line of its own.
#
# What the endpoint actually answered (checked 2026-11-09):
#   XI_29 (THYAO): 3C "Satış Gelirleri" / "Net Sales", 3D "BRÜT KAR (ZARAR)",
#     3DF "FAALİYET KARI (ZARARI)" (preceded by 3DEA "Faaliyet Karı Öncesi Diğer
#     Gelir ve Giderler", which must not match), 3L "DÖNEM KARI (ZARARI)",
#     3Z "Ana Ortaklık Payları". 3ZD "Hisse Başına Kazanç" is rounded to whole
#     lira and was 0 for a full year, so EPS is not read.
#   UFRS (GARAN, banks): no revenue or gross profit; 3Z "XXIII. NET DÖNEM
#     KARI/ZARARI", 3ZA "23.1 Grubun Karı/Zararı" (the parent's share).
ITEMS = {
    "revenue": [r"^satis gelirleri\b", r"^hasilat\b", r"^(?:net )?revenue\b", r"^(?:net )?sales\b"],
    "gross_profit": [r"^brut kar\b", r"^brut kar ?/ ?zarar", r"^gross profit\b"],
    "operating_income": [r"^esas faaliyet kari\b(?!.*oncesi)", r"^faaliyet kari\b(?!.*oncesi)",
                         r"^operating profits?\b(?!.*before)", r"^profit from operating activities\b"],
    "net_income_total": [r"^donem kari\b", r"^donem net kari\b", r"^net donem kari\b",
                         r"^donem net kar\b",
                         r"^net (?:profit|income) (?:\(loss\) )?for the period\b",
                         r"^net profit after taxes\b", r"^(?:profit|loss) for the period\b"],
    "net_income_parent": [r"^ana ortaklik paylari\b", r"equity holders of the parent",
                          r"owners of the parent", r"^grubun pay", r"^grubun kar",
                          r"^parent'?s? shares?\b", r"^group'?s? profit"],
}
_NUMBERING = re.compile(r"^(?:[ivxlc]+\.|\d+(?:\.\d+)*\.?)\s+")
_ROMAN = re.compile(r"^[ivxlc]+\.\s")

_FOLD = str.maketrans({"İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
                       "Ç": "c", "ç": "c", "Ö": "o", "ö": "o", "Ü": "u", "ü": "u",
                       "Â": "a", "â": "a", "Î": "i", "î": "i", "Û": "u", "û": "u",
                       "’": "'", "̇": None})


def fold(text):
    """Turkish-safe matching form: 'BRÜT KAR (ZARAR)' -> 'brut kar (zarar)'.
    Non-breaking spaces count as spaces (the English labels have them)."""
    return re.sub(r"\s+", " ", (text or "").translate(_FOLD).lower()).strip()


def label_key(text):
    """fold() plus the leading numbering dropped: 'XXIII. NET DÖNEM KARI' -> 'net donem kari'."""
    return _NUMBERING.sub("", fold(text))


class FactsError(Exception):
    pass


def money(x):
    """1234.5 -> '1.234,50 TL' (Turkish grouping)."""
    s = "{:,.2f}".format(float(x))
    return s.replace(",", "_").replace(".", ",").replace("_", ".") + " TL"


def mn(v):
    """TL -> '12.345,6 mn TL'."""
    if v is None:
        return "n/a"
    s = "{:,.1f}".format(v / 1e6)
    return s.replace(",", "_").replace(".", ",").replace("_", ".") + " mn TL"


def today_tsi():
    return (_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) + _dt.timedelta(hours=TR_OFFSET)).date()


# ---------------------------------------------------------------- periods

def period_list(today, count):
    """Newest quarter end on or before today, then backwards: [(2026, 9), (2026, 6), ...]."""
    y, m = today.year, (today.month - 1) // 3 * 3
    if m == 0:
        y, m = y - 1, 12
    out = []
    for _ in range(count):
        out.append((y, m))
        m -= 3
        if m == 0:
            y, m = y - 1, 12
    return out


def period_query(periods):
    return "".join("&year%d=%d&period%d=%d" % (i + 1, y, i + 1, m) for i, (y, m) in enumerate(periods))


def quarter_end(y, m):
    last = {3: 31, 6: 30, 9: 30, 12: 31}[m]
    return _dt.date(y, m, last).isoformat()


# ---------------------------------------------------------------- the answer

def check_shape(doc):
    """The answer's list of line items, or FactsError with a plain message."""
    if isinstance(doc, dict) and doc.get("ok") is False:
        raise FactsError("İş Yatırım answered an error: %s %s. Read the report on KAP instead: %s"
                         % (doc.get("errorCode") or "", doc.get("errorDescription") or "", KAP_URL))
    if not isinstance(doc, dict) or "value" not in doc:
        raise FactsError(SHAPE_HELP)
    items = doc["value"]
    if items is None:
        return []
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise FactsError(SHAPE_HELP)
    if items and not any(("itemDescTr" in i or "itemDescEng" in i) and "value1" in i for i in items[:5]):
        raise FactsError(SHAPE_HELP)
    return items


def num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(" ", "").replace(",", ""))
    except ValueError:
        return None


def is_empty(items, ncols=4):
    return not any(num(it.get("value%d" % c)) is not None for it in items for c in range(1, ncols + 1))


def income_lines(items):
    """Indexes of the income-statement lines (itemCode 3...); all lines when no codes are given."""
    idx = [n for n, i in enumerate(items) if str(i.get("itemCode") or "").startswith("3")]
    return idx if idx else list(range(len(items)))


def map_items(items):
    """{key: index into items} for the income-statement lines we know, by description."""
    allowed = set(income_lines(items))
    descs = [(label_key(i.get("itemDescTr")), label_key(i.get("itemDescEng"))) if n in allowed
             else ("", "") for n, i in enumerate(items)]
    found = {}
    for key, pats in ITEMS.items():
        start = 0
        if key == "net_income_parent":
            # the parent's share is the first one after the net income line
            if "net_income_total" not in found:
                continue
            start = found["net_income_total"] + 1
        for rx in pats:
            hit = next((n for n in range(start, len(descs))
                        if re.search(rx, descs[n][0]) or re.search(rx, descs[n][1])), None)
            if hit is not None:
                found[key] = hit
                break
    return found


def _period_from_url(url):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    out = []
    for i in range(1, 5):
        y, p = q.get("year%d" % i), q.get("period%d" % i)
        if y and p:
            out.append((int(y[0]), int(p[0])))
    return out, (q.get("companyCode") or [None])[0], (q.get("financialGroup") or [None])[0]


def normalize_answer(ans, code=None, group=None):
    """A saved answer -> {"code", "group", "periods": [(y, m)...], "value": [...]}."""
    if not isinstance(ans, dict):
        raise FactsError("offline: an answer is not a JSON object")
    periods, ucode, ugroup = [], None, None
    if ans.get("url"):
        periods, ucode, ugroup = _period_from_url(ans["url"])
    if ans.get("periods"):
        periods = [tuple(int(x) for x in p) for p in ans["periods"]]
    if not periods:
        raise FactsError("offline: a saved answer has no periods. Add \"periods\": [[2025, 9], "
                         "[2025, 6], [2025, 3], [2024, 12]] (value1..value4, in order) or the "
                         "\"url\" it came from.")
    return {"code": (ans.get("companyCode") or ucode or code or "*").upper(),
            "group": ans.get("financialGroup") or ugroup or group,
            "periods": periods, "value": check_shape(ans)}


def load_offline(path):
    """{CODE or "*": {"group", "name", "answers": [...]}} from a saved file.

    Accepted shapes:
      - a bundle from --save: {"companies": {"THYAO": {"financialGroup", "name", "answers": [...]}}}
      - one company: {"companyCode": "THYAO", "financialGroup": "XI_29", "answers": [...]}
      - a list of answers
      - one answer: {"value": [...], "periods": [[2025, 9], ...]} or with "url"
    Every answer needs "periods" (or the "url" it was fetched from)."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise FactsError("offline: cannot read %s: %s" % (path, exc))
    out = {}

    def add(code, group, name, answers):
        for a in answers:
            n = normalize_answer(a, code, group)
            slot = out.setdefault(n["code"], {"group": n["group"], "name": name, "answers": []})
            slot["answers"].append(n)
            slot["group"] = slot["group"] or n["group"]

    if isinstance(doc, dict) and isinstance(doc.get("companies"), dict):
        for code, c in doc["companies"].items():
            add(code, c.get("financialGroup"), c.get("name"), c.get("answers") or [c])
    elif isinstance(doc, dict) and isinstance(doc.get("answers"), list):
        add(doc.get("companyCode"), doc.get("financialGroup"), doc.get("name"), doc["answers"])
    elif isinstance(doc, list):
        add(None, None, None, doc)
    elif isinstance(doc, dict) and "value" in doc:
        add(doc.get("companyCode"), doc.get("financialGroup"), doc.get("name"), [doc])
    else:
        raise FactsError("offline: %s is not a saved MaliTablo answer or bundle" % path)
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, pause=1.0):
        self.pause, self._last = pause, 0.0

    def mali(self, code, group, periods):
        url = MALI_URL % (urllib.parse.quote(code), urllib.parse.quote(group), period_query(periods))
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                                   "Accept": "application/json",
                                                   "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
        except urllib.error.HTTPError as exc:
            raise FactsError("İş Yatırım answered HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise FactsError("could not reach İş Yatırım: %s" % exc.reason)
        try:
            doc = json.loads(data.decode("utf-8-sig"))
        except ValueError:
            raise FactsError(SHAPE_HELP)
        return {"code": code, "group": group, "periods": list(periods), "url": url,
                "value": check_shape(doc)}


def fetch_company(fetcher, code, quarters, group=None, today=None):
    """Enough cumulative periods for `quarters` discrete quarters plus a year of YoY."""
    need = quarters + 5 + 1  # +4 for YoY, +1 to derive the oldest, +1 if the newest is not out yet
    need = (need + 3) // 4 * 4
    periods = period_list(today or today_tsi(), need)
    chunks = [periods[i:i + 4] for i in range(0, len(periods), 4)]
    groups = [group] if group else list(GROUPS)
    chosen, answers = None, []
    for chunk in chunks:
        if chosen is None:
            for g in groups:
                ans = fetcher.mali(code, g, chunk)
                if not is_empty(ans["value"], len(chunk)):
                    chosen = g
                    answers.append(ans)
                    break
            if chosen is None:
                raise FactsError("İş Yatırım returned no numbers for %s (tried financialGroup %s). "
                                 "Check the code, or read the report on KAP: %s"
                                 % (code, ", ".join(groups), KAP_URL))
        else:
            answers.append(fetcher.mali(code, chosen, chunk))
    return chosen, answers


# ---------------------------------------------------------------- analysis

def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def live_columns(ans):
    """1-based columns of an answer that carry at least one non-zero number."""
    return [c for c in range(1, len(ans["periods"]) + 1)
            if any(num(it.get("value%d" % c)) for it in ans["value"])]


def raw_top(answers, n=20):
    """The first income-statement lines with a non-zero number, newest period first,
    for layouts we cannot map (banks). Values are year to date."""
    cols = [(tuple(per), ans, col) for ans in answers for col, per in enumerate(ans["periods"], 1)
            if col in live_columns(ans)]
    for per, ans, col in sorted(cols, key=lambda x: x[0], reverse=True):
        lines = [ans["value"][k] for k in income_lines(ans["value"])]
        top = [it for it in lines if _ROMAN.match(fold(it.get("itemDescTr") or ""))]
        if top:  # the bank layout numbers its main lines I., II., ... XXIII.: keep those
            lines = top
        rows = [{"desc": (it.get("itemDescTr") or it.get("itemDescEng") or "").strip(),
                 "desc_en": (it.get("itemDescEng") or "").strip() or None,
                 "code": it.get("itemCode"), "value": num(it.get("value%d" % col))}
                for it in lines if num(it.get("value%d" % col))]
        if rows:
            return {"period": quarter_end(*per), "lines": rows[:n]}
    return None


def analyse(answers, code, quarters=8, name=None, group=None):
    """Every number in the report, as a dict."""
    cum = {}       # (y, m) -> {key: cumulative value}
    labels = {}    # key -> the description used
    for ans in answers:
        found = map_items(ans["value"])
        live = live_columns(ans)
        for key, idx in found.items():
            it = ans["value"][idx]
            labels.setdefault(key, (it.get("itemDescTr") or it.get("itemDescEng") or "").strip())
            for col, per in enumerate(ans["periods"], 1):
                if col not in live:
                    continue  # a period not reported yet: every line is null or 0
                v = num(it.get("value%d" % col))
                if v is not None:
                    cum.setdefault(tuple(per), {})[key] = v
    base = {"entity": name or code, "code": code, "group": group, "rows": [], "ttm": {},
            "items": labels, "raw": None, "net_income_basis": None}
    has = lambda k: any(k in v for v in cum.values())
    ni_key = "net_income_parent" if has("net_income_parent") else "net_income_total"
    if not has("revenue"):
        base["raw"] = raw_top(answers)  # banks: no revenue line, show what is there
    if not has("revenue") and not has(ni_key):
        return base
    base["net_income_basis"] = "parent" if ni_key == "net_income_parent" else "total"

    # discrete quarters from year-to-date figures
    disc = {}  # (y, m) -> {key: (value, derived)}
    for (y, m), vals in cum.items():
        prev = cum.get((y, m - 3)) if m > 3 else None
        for key, v in vals.items():
            if m == 3:
                disc.setdefault((y, m), {})[key] = (v, False)
            elif prev is not None and key in prev:
                disc.setdefault((y, m), {})[key] = (v - prev[key], True)

    def g(per, key):
        return (disc.get(per, {}).get(key) or (None, False))[0]

    pers = sorted((p for p, d in disc.items() if "revenue" in d or ni_key in d), reverse=True)
    rows = []
    for (y, m) in pers[:quarters]:
        r, gp = g((y, m), "revenue"), g((y, m), "gross_profit")
        oi, ni, eps = g((y, m), "operating_income"), g((y, m), ni_key), g((y, m), "eps")
        prior = (y - 1, m)
        rows.append({
            "end": quarter_end(y, m), "fiscal": "Q%d %d" % (m // 3, y),
            "derived": m != 3,
            "revenue": r, "gross_profit": gp, "operating_income": oi, "net_income": ni,
            "net_income_total": g((y, m), "net_income_total"),
            "eps": round(eps, 2) if eps is not None else None,
            "gross_margin": margin(gp, r), "operating_margin": margin(oi, r),
            "net_margin": margin(ni, r),
            "revenue_yoy": pct(r, g(prior, "revenue")),
            "net_income_yoy": pct(ni, g(prior, ni_key)),
            "eps_yoy": pct(eps, g(prior, "eps")),
            "ytd_revenue": cum.get((y, m), {}).get("revenue"),
            "ytd_net_income": cum.get((y, m), {}).get(ni_key),
        })
    ttm = {}
    for key, out_key in (("revenue", "revenue"), (ni_key, "net_income"), ("eps", "eps")):
        last = [p for p in pers if key in disc[p]][:4]
        if len(last) == 4 and (last[0][0] * 12 + last[0][1]) - (last[3][0] * 12 + last[3][1]) == 9:
            ttm[out_key] = sum(disc[p][key][0] for p in last)
    if "eps" in ttm:
        ttm["eps"] = round(ttm["eps"], 2)
    base["rows"], base["ttm"] = rows, ttm
    return base


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    out = []
    label = "Quarter ending %s (%s%s)" % (cur["end"], cur["fiscal"],
                                          ", year-to-date minus the quarter before" if cur["derived"] else "")
    parts = []
    if cur["revenue"] is not None:
        parts.append("revenue %s" % mn(cur["revenue"]))
        if cur["revenue_yoy"] is not None:
            parts[-1] += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    who = "net income to the parent" if result.get("net_income_basis") == "parent" else "net income"
    parts.append("%s %s" % (who, mn(cur["net_income"])))
    if cur["net_income_yoy"] is not None:
        parts[-1] += " (%+.1f%% vs a year earlier)" % cur["net_income_yoy"] if cur["revenue"] is None \
            else " (%+.1f%%)" % cur["net_income_yoy"]
    line = "%s: %s" % (label, ", ".join(parts))
    if cur["eps"] is not None:
        line += ", EPS %s" % money(cur["eps"])
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")
    for key, name in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                      ("net_margin", "Net margin")):
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        out.append(s + ".")
    if cur["net_income"] is not None and cur["operating_income"] is not None \
            and cur["net_income"] > cur["operating_income"]:
        out.append("Net income is above operating profit: it includes items outside the main "
                   "business (investment income, FX gains, monetary gain under inflation "
                   "accounting, deferred tax). Check the report on KAP before reading it as growth.")
    if prev and cur["revenue"] is not None and prev["revenue"]:
        out.append("Revenue %+.1f%% vs the quarter before (many businesses are seasonal: "
                   "compare with a year earlier first)." % pct(cur["revenue"], prev["revenue"]))
    return out


# ---------------------------------------------------------------- output

def _m(v):
    if v is None:
        return "-"
    return "{:,.0f}".format(v / 1e6).replace(",", ".")


def _p(v):
    return "-" if v is None else "%+.1f%%" % v if v < 0 or v > 0 else "0.0%"


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def _raw_block(raw):
    if not raw:
        return ["  (no lines with numbers)"]
    out = ["  period ending %s, mn TL" % raw["period"]]
    for ln in raw["lines"]:
        out.append("  %-60s %12s" % ((ln["desc"] or "?")[:60], _m(ln["value"])))
    return out


def render(ticker, result):
    head = "%s (%s)" % (result["entity"], ticker) if result["entity"] != ticker else ticker
    out = ["%s, financialGroup %s. mn TL (TL millions). Source: %s; "
           "official figures are on KAP." % (head, result.get("group") or "?", SOURCE), ""]
    if not result["rows"]:
        out.append("Could not map revenue or net income in this layout (banks and insurers "
                   "use another one). Raw top income-statement lines as İş Yatırım gives them:")
        out += _raw_block(result.get("raw"))
        out.append("")
        out.append("Financial reports (official): %s (search the code, type Finansal Rapor)" % KAP_URL)
        return "\n".join(out) + "\n"
    show_eps = any(r["eps"] is not None for r in result["rows"])
    hdr = "%-11s %-8s %10s %8s %7s %7s %7s %10s %8s" % (
        "quarter end", "quarter", "revenue", "rev yoy", "gross", "oper", "net", "net inc", "NI yoy")
    if show_eps:
        hdr += " %8s %7s" % ("EPS", "EPS yoy")
    out.append(hdr)
    for r in result["rows"]:
        line = "%-11s %-8s %10s %8s %7s %7s %7s %10s %8s" % (
            r["end"], r["fiscal"], _m(r["revenue"]), _p(r["revenue_yoy"]),
            _pm(r["gross_margin"]), _pm(r["operating_margin"]), _pm(r["net_margin"]),
            _m(r["net_income"]), _p(r["net_income_yoy"]))
        if show_eps:
            line += " %8s %7s" % ("-" if r["eps"] is None else "%.2f" % r["eps"], _p(r["eps_yoy"]))
        out.append(line + ("  derived" if r["derived"] else ""))
    t = result["ttm"]
    if t:
        out.append("")
        s = "Last 4 quarters: revenue %s, net income %s" % (mn(t.get("revenue")), mn(t.get("net_income")))
        if t.get("eps") is not None:
            s += ", EPS %s" % money(t["eps"])
        out.append(s + ".")
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    out.append("")
    if result.get("raw") is not None:
        out.append("No revenue line in this layout (banks: no sales, so no margins). Top "
                   "income-statement lines as İş Yatırım gives them, year to date:")
        out += _raw_block(result["raw"])
        out.append("")
    if result.get("net_income_basis") == "parent":
        out.append("Net income is the parent's share (%s)."
                   % (result.get("items", {}).get("net_income_parent") or "Ana Ortaklık Payları"))
    out.append("Statements are year to date; \"derived\" quarters (Q2-Q4) are the cumulative "
               "figure minus the one before (under inflation accounting, an approximation).")
    out.append("Financial reports (official): %s (search the code, type Finansal Rapor). "
               "İş Yatırım's endpoint is unofficial: check the numbers there before relying on them."
               % KAP_URL)
    return "\n".join(out) + "\n"


def metrics_row(ticker, result):
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    return {"ticker": ticker, "name": result["entity"], "financial_group": result.get("group"),
            "currency": "TRY", "period_end": cur.get("end"), "quarter": cur.get("fiscal"),
            "revenue_ttm": t.get("revenue"), "net_income_ttm": t.get("net_income"),
            "eps_ttm": t.get("eps"), "net_income_basis": result.get("net_income_basis"),
            "revenue_growth_yoy": cur.get("revenue_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "net_income_growth_yoy": cur.get("net_income_yoy"),
            "gross_margin": cur.get("gross_margin"), "operating_margin": cur.get("operating_margin"),
            "net_margin": cur.get("net_margin"), "source": SOURCE}


def load_names(path="trader/universe.txt"):
    out = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    code, _, name = line.partition("|")
                    if code.strip():
                        out[code.strip().upper()] = name.strip() or None
    return out


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Turkish text (TSİ) on a non-UTF-8 Windows pipe
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tickers", nargs="*", help="BIST codes, e.g. THYAO ASELS")
    ap.add_argument("--quarters", type=int, default=8)
    ap.add_argument("--group", choices=GROUPS, help="financialGroup: XI_29 (industrials) or UFRS "
                                                    "(banks, financials); default tries both")
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per company into this JSON")
    ap.add_argument("--universe", default="trader/universe.txt", help="CODE|Name lines, for names")
    ap.add_argument("--offline", metavar="FILE", help="read a saved answer or --save bundle")
    ap.add_argument("--save", metavar="FILE", help="write the raw answers as a bundle for --offline")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.tickers:
        ap.print_help()
        return 2
    offline = None
    if args.offline:
        try:
            offline = load_offline(args.offline)
        except FactsError as exc:
            print("facts: %s" % exc, file=sys.stderr)
            return 2
    names = load_names(args.universe)
    fetcher = Fetcher()
    results, rows, failed, bundle = [], [], 0, {}
    for t in args.tickers:
        code = t.strip().upper()
        if code.endswith(".IS"):
            code = code[:-3]
        try:
            if offline is not None:
                slot = offline.get(code) or (offline.get("*") if len(args.tickers) == 1 else None)
                if not slot:
                    raise FactsError("offline: no saved answer for %s in %s" % (code, args.offline))
                group, answers = args.group or slot["group"], slot["answers"]
                name = names.get(code) or slot.get("name")
            else:
                group, answers = fetch_company(fetcher, code, args.quarters, args.group)
                name = names.get(code)
            result = analyse(answers, code, quarters=args.quarters, name=name, group=group)
        except FactsError as exc:
            print("facts: %s: %s" % (code, exc), file=sys.stderr)
            failed += 1
            continue
        bundle[code] = {"name": name, "financialGroup": group,
                        "answers": [{"periods": [list(p) for p in a["periods"]],
                                     "url": a.get("url"), "value": a["value"]} for a in answers]}
        results.append({"ticker": code, "result": result, "changes": changes(result),
                        "kap": KAP_URL, "source": SOURCE})
        if result["rows"]:
            rows.append(metrics_row(code, result))
        if not args.json:
            sys.stdout.write(render(code, result))
            sys.stdout.write("\n")
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    if args.save and bundle and offline is None:
        folder = os.path.dirname(args.save)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump({"saved": today_tsi().isoformat(), "source": SOURCE, "companies": bundle},
                      fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        if not args.json:
            print("saved the raw answers to %s (read them back with --offline)" % args.save)
    if args.metrics and rows:
        existing = {}
        if os.path.isfile(args.metrics):
            with open(args.metrics, encoding="utf-8") as fh:
                existing = {r["ticker"]: r for r in json.load(fh).get("companies", [])}
        for r in rows:
            existing[r["ticker"]] = r
        folder = os.path.dirname(args.metrics)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.metrics, "w", encoding="utf-8") as fh:
            json.dump({"as_of": today_tsi().isoformat(), "currency": "TRY",
                       "companies": sorted(existing.values(), key=lambda r: r["ticker"])},
                      fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    unmapped = sum(1 for r in results if not r["result"]["rows"])
    return 0 if results and not failed and not unmapped else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())
