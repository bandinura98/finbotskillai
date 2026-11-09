---
name: trader-news
description: >-
  News Monitor for Borsa İstanbul (BIST). Tracks breaking press headlines on
  every BIST share you watch and flags what moves it: Google News in Turkish
  and Yahoo Finance headlines per code (THYAO, ASELS, GARAN...), and how far
  each share is from yesterday's close. Points to KAP for official
  disclosures. Shows only what is new since the last check, newest first, in
  Istanbul time (TSİ), tags each item (bilanço/earnings, temettü, bedelsiz /
  bedelli, pay geri alım, birleşme/devralma, ihale/sözleşme, SPK, KAP...) and
  never repeats one. Use when the user says "THYAO'da haber var mı", "neden
  yükseliyor/düşüyor", "izleme listemde ne haber var", "sabah bülteni",
  "açılış öncesi haberler", "haberleri takip et", "any news on THYAO", or
  after /trader-scanner finds setups.
argument-hint: "[BIST codes]"
---

# trader-news

**Talk to the user in Turkish.** Code, file names and CLI flags stay as they
are; your explanations, briefs and questions are in Turkish.

A chart can look perfect and still be about to gap on a bilanço, a bedelsiz
announcement, an SPK action or a VBTS measure (brüt takas, tek fiyat). Before a
setup becomes a trade, this skill reads the press feeds that carry those first,
for every share on the watchlist, and tells you only what is new. It also
prints how far each share has moved today, so a headline and a move sit side
by side.

**KAP is the official source.** Company disclosures (özel durum açıklamaları,
financial reports, capital increases, buybacks, new business relations) are
published on the Kamuyu Aydınlatma Platformu: https://www.kap.org.tr/tr/bildirim-sorgu .
This tool reads press headlines, not KAP; the tool prints that pointer once
per run.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                          # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers THYAO ASELS --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-google --json
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner); bare codes like `THYAO` |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (trader-broker records fills there) |
| `trader/universe.txt` | `CODE\|Name` lines (from `scan.py universe`): adds the company name to the Google search and to mention matching |
| Google News RSS (Turkish) | `https://news.google.com/rss/search?q=THYAO+hisse&hl=tr&gl=TR&ceid=TR:tr` (skip with `--no-google`) |
| Yahoo Finance headline RSS | `https://feeds.finance.yahoo.com/rss/2.0/headline?s=THYAO.IS&region=US&lang=en-US` |
| Yahoo Finance chart endpoint | the last price of `THYAO.IS` against yesterday's close (unofficial, can lag) |
| `trader/state/news.json` | what has been shown (kept 30 days) |

What the feeds actually carry (checked 2026-11-09): Google News returns
Turkish press headlines (Hisse.net, Ekonomim, Paratic, Rota Borsa, Bloomberg
HT ...), many of them roundups that name several codes ("THYAO, ASELS ve TUPRS
dahil 20 hisse için hedef fiyat"), a few dozen items (56 that morning)
reaching back years; the
window (`--hours`, default 48) keeps the recent ones. Yahoo's `.IS` feed is
English wire copy (aircraft orders, leases, partner companies' earnings),
often days old and sometimes only loosely about the share. The same story is
shown once when the two feeds (or two codes' searches) carry it with the same
link or the same normalized title on the same TSİ day; a Turkish and an
English write-up of one event are different titles and both show.

Before the open (or on a holiday) Yahoo's chart is the last session's: the
price line then carries that session's date and "(last session)" instead of
a move "today".

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story and read it. Quote the source and link it. Never
  summarise a paywalled story you could not read: say so.
- **KAP beats headlines.** If a headline and a KAP disclosure disagree, KAP is
  what the company told the market. For anything that changes the trade
  (bilanço, sermaye artırımı, pay geri alım, SPK, işlem durdurma), check KAP
  and give the user that link.
- **Say what it touches.** Every item lists the codes it touches and its tags:
  earnings (bilanço, kâr/zarar, finansal sonuç), dividend (temettü), capital
  (bedelsiz, bedelli, sermaye artırımı, halka arz), buyback (pay geri alım),
  deal (birleşme, devralma, satın alma), contract (yeni iş ilişkisi,
  sözleşme, ihale), regulator (SPK, soruşturma, ceza), kap, analyst (hedef
  fiyat), people, halt (işlem durdurma, devre kesici, brüt takas, tek fiyat,
  VBTS), limit (tavan, taban), risk (konkordato, iflas), macro (TCMB, faiz,
  enflasyon). Matching ignores case and Turkish letters (BİLANÇO, bilanço and
  bilanco are the same). Items marked `!` carry an earnings, capital,
  regulator, halt or risk tag.
- **MOVING** marks a share 3 % or more from yesterday's close (`--move-pct`).
  BIST has ±10 % daily limits (taban/tavan): a share near either is worth
  saying out loud. A move with no headline is worth saying too.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the share will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. Only items that were shown are remembered, so a wider
   `--hours` later still brings up older ones not seen yet. `--all` shows
   items already seen, `--json` gives everything machine readable, `--dry-run`
   leaves the state alone.
2. Read the items that matter: every `!` item, anything tagged earnings,
   capital, halt, regulator, kap or risk, and every MOVING code first.
3. Write a short brief in Turkish, per code: what happened, the source and
   link, the move, and one line on why it matters to the setup (for example
   "bilanço bu akşam açıklanacak: yarın sabah gap stopun ötesine atlayabilir").
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: a bilanço date inside the trade, a VBTS measure, a bedelli
   sermaye artırımı. Point to KAP. Never act on a headline alone.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before the 10:00 TSİ open.

## Example

A real check on THYAO, Monday 2026-11-09 08:37 TSİ, before the open
(`--hours 120`):

```
THYAO  299.25 +0.76% vs the close before 297.00 at 2026-11-06 18:09 TSİ (last session)

  2026-11-08 15:54 TSİ  THYAO       Google News/Hisse.net ASELS, SASA ve SISE dahil 17 hissede özel emir işlemi...
      https://news.google.com/rss/articles/...
  2026-11-08 12:30 TSİ  THYAO       Google News/Hisse.net Borsada 46 hisseye hedef fiyat! THYAO, ASELS ve TUPRS için...
      tags: analyst  https://news.google.com/rss/articles/...
! 2026-11-06 14:40 TSİ  THYAO       Google News/Türkiye Gazetesi 1,3 milyar dolar kâr açıkladı! 9 kurumdan Türk Hava Yolları (THYAO) hissesi için hedef fiyat
      tags: earnings, analyst  https://news.google.com/rss/articles/...
  2026-11-04 22:36 TSİ  THYAO       Google News/Paratic Haber BofA tavan olan THYAO’da 1,3 milyar TL’lik alım yaptı
      tags: limit  https://news.google.com/rss/articles/...
Resmî bildirimler: https://www.kap.org.tr/tr/bildirim-sorgu (official disclosures are on KAP; ...)
```

The brief, in Turkish: the 3rd-quarter report and the brokers' new target
prices after it (open the stories, then the financial report on KAP), the
limit-up day on 4 November, Friday's close; that the market has not opened
yet; and that nothing here is investment advice. When a code has no news, say
so rather than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline. Yatırım tavsiyesi değildir.
- Never hammers a server: one request per feed per code and a pause between
  calls.

## Safety

Education and tooling, not investment advice (yatırım tavsiyesi değildir).
This skill only reads and reports. Nothing places an order: `/trader-broker`
only prints a manual order slip (emir fişi) after `/trader-risk` passes and the
user types an explicit yes.
