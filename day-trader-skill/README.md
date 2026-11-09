# Day Trader skill: Borsa İstanbul (BIST) uyarlaması

Claude için 10 gün içi işlem (day trading) skill'i: grafik tarama, haber takibi,
giriş/çıkış zamanlaması, risk ve pozisyon büyüklüğü hesabı, ve para koymadan
önce işlemi geçmiş veriyle binlerce kez tekrar oynatan "Zaman Makinesi".

Bu repo, Jake Schincariol'un MIT lisanslı
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
projesinin Borsa İstanbul'a uyarlanmış halidir. Orijinal ABD sürümü `main`
dalında, BIST sürümü `bist` dalındadır.

> **Yatırım tavsiyesi değildir.** Bu yazılım herkese açık veriyi okuyup
> aritmetik yapar. Gün içi işlem yapanların çoğu para kaybeder. Kararı siz
> verirsiniz; gerekirse lisanslı bir danışmana başvurun.

## Ne değişti (ABD → BIST)

| Konu | Orijinal (ABD) | BIST sürümü |
| --- | --- | --- |
| Saat | New York (ET), yaz saati | İstanbul (TSİ), sabit UTC+3 |
| Seans | 09:30–16:00 | Sürekli işlem 10:00–18:00 (açılış seansı 09:40–09:55, kapanış seansı 18:00–18:10) |
| Yarım gün | 13:00 kapanış | Arefe ve 28 Ekim: sürekli işlem 12:30'da biter |
| Tatiller | NYSE takvimi | BIST 2026 resmi takvimi; 2027 bayramları **tahmini** |
| Semboller | TSLA | THYAO (Yahoo'da `THYAO.IS`, araçlar `.IS` ekini kendisi ekler) |
| Hisse listesi | Nasdaq Trader | `trader-scanner/bist_universe.txt` (düzenlenebilir) |
| Fiyat adımı | 0,01 $ | BIST fiyat adımı tablosu (0,01 – 2,50 TL) |
| Günlük limit | yok | Taban/tavan ±%10: stop tabanın altında, hedef tavanın üstünde olamaz |
| Maliyet | komisyonsuz | Komisyon + BSMV + borsa payı, risk ve lot hesabına dahil |
| Haberler | Yahoo + SEC 8-K | Google Haberler (Türkçe) + Yahoo; resmî bildirimler için KAP linki |
| Bilanço | SEC XBRL | İş Yatırım mali tablo verisi (resmî olmayan uç nokta) |
| Aracı kurum | Alpaca API | Emir gönderilmez: onayınızdan sonra kendi aracı kurum uygulamanıza gireceğiniz **emir fişi** basılır |
| Kongre işlemleri (pelosi) | var | Kaldırıldı (ABD'ye özel) |
| PDT kuralı | var | BIST'te yok, kapalı |

Bütün piyasa kuralları tek bir yerde yazılı: [docs/BIST-SPEC.md](docs/BIST-SPEC.md).

## Kurulum

Python 3.8 veya üstü yeter, `pip install` gerekmez.

```bash
git clone -b bist <bu-reponun-adresi> day-trader-skill
cp -r day-trader-skill/trader-* ~/.claude/skills/
```

Windows'ta klasörleri `%USERPROFILE%\.claude\skills\` içine kopyalayın. Sonra
Claude Code'da `/trader-scanner`, `/trader-news` gibi komutlarla kullanılır.
Sadece bir projede kullanmak için aynı klasörleri o projenin
`.claude/skills/` klasörüne kopyalayın.

## On skill

| komut | iş |
| --- | --- |
| `/trader-scanner` | BIST hisselerinde kurulum tarar: açılış aralığı kırılımı (ORB), bayrak, aşırı satım dönüşü. |
| `/trader-news` | İzlediğiniz hisselerin haberlerini takip eder, fiyatı oynatabilecekleri işaretler. |
| `/trader-entry` | Kurulumu mum mum izler, giriş anını söyler. |
| `/trader-exit` | Girmeden önce stop ve hedefi belirler (fiyat adımına yuvarlanmış, taban/tavan kontrollü). |
| `/trader-timemachine` | İşlemi 10.000 kez simüle eder ve aynı kuralı geçmiş seanslarda tekrar oynatır. |
| `/trader-risk` | Her emri limitlerinize göre kontrol eder: GEÇER / ENGELLE. |
| `/trader-budget` | Bütçenize ve işlem başı maksimum zararınıza göre lot hesaplar (maliyetler dahil). |
| `/trader-broker` | Onayladığınız emir için emir fişi basar; hiçbir yere emir göndermez. |
| `/trader-alerts` | Fiyat, stop, hedef ve seans kapanışı için alarm. |
| `/trader-analyst` | Çeyreklik mali tabloları okur, neyin değiştiğini özetler. |

## Akış

Her şey projenizdeki `trader/` klasörüne yazılır, her skill bir öncekinin
çıktısını okur:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (tek işlem)
alerts: stop, hedef, 17:50'de pozisyonu kapat                                (pozisyon açıkken)
news / analyst                                                                (düzenli aralıklarla)
```

## Araçlar

```bash
python trader-scanner/scan.py universe                  # bist_universe.txt -> trader/universe.txt
python trader-scanner/scan.py daily                     # seans öncesi günlük kurulumlar
python trader-scanner/scan.py orb --at 10:30            # 10:00-10:15 açılış aralığı kırılımları
python trader-news/news.py --tickers THYAO --hours 12   # haberler ve günün hareketi
python trader-entry/entry.py check THYAO --setup orb    # bekliyor / tetiklendi / süresi doldu
python trader-exit/exit.py plan --stop or-mid --target 1R --write
python trader-timemachine/timemachine.py run --write
python trader-budget/size.py --write
python trader-risk/check.py
python trader-broker/ticket.py show
python trader-alerts/alerts.py clock                    # borsa açık mı?
python trader-analyst/facts.py THYAO
```

## Dikkat

* **Veri gecikmeli ve resmî değil.** Yahoo Finance'in BIST verisi genellikle
  ~15 dakika gecikmelidir ve resmî bir API değildir. İş Yatırım uç noktası da
  resmî değildir, biçimi değişebilir. Önemli her şeyi aracı kurumunuzun
  ekranından kontrol edin.
* **Emir gönderilmez.** `/trader-broker` sadece `/trader-risk`'ten geçmiş ve
  sizin "evet" dediğiniz emir için bir fiş basar; emri kendi uygulamanıza siz
  girersiniz.
* **Stop bir plandır, garanti değildir.** Hızlı piyasada veya tabana kilitlenen
  bir hissede stop emri hiç dolmayabilir.
* **Takvim.** 2027 Ramazan ve Kurban Bayramı tarihleri tahminidir. Borsa
  İstanbul 2027 takvimini yayımlayınca her script'teki `BIST_HOLIDAYS` /
  `BIST_HALF_DAYS` sözlüklerini güncelleyin.
* **Fiyat adımları ve maliyetler.** Fiyat adımı tablosunu Pay Piyasası
  Prosedürü §5.1.9 ile, komisyon oranını kendi aracı kurumunuzla kontrol edip
  `trader/config.json`'a yazın.
* **Vergi.** Bireysel yatırımcının BIST hisse kazancında stopaj şu an %0'dır;
  bu vergi tavsiyesi değildir.

MIT. Orijinal: [Jake Schincariol](https://opusjake.ai).
