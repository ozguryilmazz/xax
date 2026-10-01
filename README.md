# Futures Trader

Binance USDⓈ-M vadeli işlemler için Python trade programı.

- **1. aşama:** paper trading (sanal bakiye, gerçek piyasa verisi)
- **2. aşama:** Binance Futures Testnet
- **3. aşama:** canlı hesap

Üç modda da aynı işlem motoru çalışır; değişen tek şey emirlerin gittiği "broker"dır.

## Kurulum

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # ayarları düzenleyin
python run.py               # tarayıcıda http://127.0.0.1:8000
```

İnternet veya Binance erişimi olmadan denemek için `.env` içinde
`TRADER_MARKET_SOURCE=simulated` ayarlayın. Bu durumda rastgele üretilmiş fiyatlar kullanılır.

Testler: `pip install -r requirements-dev.txt && pytest`

## İşlem kuralları

| Kural | Davranış |
|---|---|
| Emir tipi | Limit (GTC). Limit fiyatı piyasadan iyiyse hemen dolar (taker) |
| Teminat modu | Isolated |
| Stop loss | **Teminatın %30'u** kayıp. 10 USDT, 5x, giriş 100 → SL 94 (−3 USDT) |
| Take profit | **Teminatın %15'i** kazanç. Aynı örnekte → TP 103 (+1,5 USDT) |
| SL/TP ne zaman kurulur | Emir verildiği anda, limit fiyatına göre hesaplanır |
| SL/TP değiştirilebilir mi | Hayır; iptal de edilemez. Teminat eklense de değişmez |
| SL/TP Binance'te görünür mü | Hayır. Bot fiyatı izler ve tetiklenince reduce-only MARKET emirle kapatır |
| Acil durum stopu | Binance'e gönderilen tek koruma: en uzak SL ile likidasyon fiyatının tam ortası. Bot kapalıyken pozisyonu korur. Teminat veya bacak eklenince otomatik taşınır |
| Aynı coine 2. emir | Mevcut pozisyona "bacak" olarak eklenir. Her bacağın teminatı, kaldıracı ve SL/TP'si kendine aittir. Ortalama giriş ve başa baş yeniden hesaplanır |
| Ters yönde emir | Reddedilir (Binance one-way modu pozisyonları netler) |
| Teminat ekleme | Likidasyon fiyatı uzaklaşır; SL/TP aynı kalır |
| Bekleyen emir iptali | Yapılabilir; sadece dolmamış kısım iptal olur |
| Elle kapatma | "Kapat" butonuyla tüm pozisyon piyasa fiyatından kapatılabilir |

Bu oranlarla bir kayıp iki kazancı siler. Komisyonlar hariç başa baş için bile işlemlerin
yaklaşık %67'sinin kârla kapanması gerekir (İstatistik sekmesinde takip edilir).

## Risk limitleri (`.env`)

- `MAX_DAILY_LOSS_USDT`: UTC gün başından beri gerçekleşen zarar bu tutara ulaşınca yeni emir verilemez
- `MAX_OPEN_POSITIONS`: aynı anda pozisyon/emir olabilecek en fazla coin sayısı
- `MAX_LEVERAGE`, `MAX_MARGIN_PER_ORDER`

## Ekran

- **Coin listesi:** Binance'te son 24 saatte en çok işlem gören 200 USDT perpetual coin. Fiyat son
  değişimde yükseldiyse yeşil, düştüyse kırmızıdır. Tıklanan coin seçili pencereye yüklenir.
- **Yan yana iki grafik penceresi (A / B):** mum grafik; 1, 5, 10, 15, 30 dakika; 1, 2, 4, 6, 12 saat;
  1 gün, 1 hafta ve 1 ay aralıkları. Saniyede bir güncellenir. 10 dakikalık mumlar Binance'te
  olmadığı için 1 dakikalık mumlardan üretilir.
- **Emir paneli:** emrin hangi pencerenin coinine verileceği "Pencere A/B" ile seçilir. Emir
  verilmeden önce SL, TP, tahmini likidasyon ve acil durum stopu önizlenir.
- **Grafikte pozisyon:** başa baş (sarı), her bacağın SL'si (kırmızı) ve TP'si (yeşil),
  likidasyon (mor) ve acil durum stopu (turuncu) gösterilir. Başa baştan SL'ye kadar olan alan
  kırmızıya, TP'ye kadar olan alan yeşile boyanır.
- **Sinyal:** kapanmış mumda fiyat değişimi (önceki kapanışa göre, mutlak değer) bir önceki
  mumdan büyük ama USDT hacmi düşükse turuncu nokta konur. Yükseliş mumunda mumun üstünde,
  düşüş mumunda altında görünür. Her mumun fiyat ve hacim değişim yüzdesi, mumun üzerine gelince
  sol üstte yazar. Sinyaller sadece açık grafiklerde hesaplanır.
- **Destek/direnç:** pivot tepe/dipler ATR'ye göre kümelenir. Güç puanı test sayısı,
  reddedilmeler, hacim ve yakınlıktan oluşur. Her yönde en güçlü 3 seviye çizilir; çizgi kalınlığı
  gücü gösterir.
- **Alt sekmeler:** pozisyonlar (bacaklarıyla), bekleyen emirler, işlem geçmişi, istatistik
  (başarı oranı, net K/Z, komisyon, funding), olay günlüğü.

## Paper trading gerçekçiliği

- Maker/taker komisyonu (varsayılan %0,02 / %0,05)
- Limit emir, fiyat limit seviyesine değince dolar
- SL/TP piyasa emriyle o anki fiyattan kapanır (kayma dahil)
- Funding her funding saatinde mark fiyat × miktar × oran olarak isolated teminattan düşülür
- Likidasyon, Binance'in isolated formülü ve bakım teminatı kademeleriyle mark fiyatına göre
  hesaplanır

## Kalıcılık ve bildirim

- Durum `data/trader.db` (SQLite) dosyasında tutulur. Program yeniden başlatılınca açık
  pozisyonlar, bekleyen emirler ve SL/TP'ler geri yüklenir. Paper, testnet ve canlı kayıtları ayrıdır.
- `TRADER_TELEGRAM_BOT_TOKEN` ve `TRADER_TELEGRAM_CHAT_ID` tanımlanırsa dolum, SL/TP, acil stop,
  likidasyon ve teminat ekleme olaylarında Telegram mesajı gönderilir.

## Mimari

```
trader/
  config.py              ayarlar (.env)
  risk_math.py           SL/TP, likidasyon, başa baş, acil stop hesapları
  intervals.py           zaman aralıkları (10m → 1m birleştirme)
  analysis/signals.py    fiyat/hacim uyumsuzluk sinyali
  analysis/levels.py     destek/direnç
  market/hub.py          piyasa verisi merkezi, top 200, mum serileri
  market/binance_market.py  Binance kaynağı (WebSocket: !ticker@arr, !markPrice@arr@1s, kline)
  market/sim_market.py   internetsiz simülasyon kaynağı
  binance/rest.py        imzalı REST istemcisi (emirler)
  binance/ws.py          piyasa ve kullanıcı WebSocket'leri (otomatik yeniden bağlanma)
  trading/manager.py     emir/pozisyon/bacak yönetimi, SL/TP takibi, risk limitleri
  trading/broker.py      broker arayüzü + PaperBroker
  trading/live_broker.py Binance broker'ı (testnet/canlı)
  storage.py             SQLite
  notify.py              Telegram
  server.py              FastAPI + tarayıcıya WebSocket
  static/                arayüz (TradingView Lightweight Charts)
```

- Piyasa verisi tamamen WebSocket'ten gelir. REST sadece başlangıçta, grafik geçmişi
  yüklenirken ve emirlerde kullanılır; böylece Binance istek limitlerine takılınmaz.
- Arayüz sunucuyla tek bir WebSocket üzerinden konuşur; sunucu saniyede bir güncelleme gönderir.

## Testnet / canlıya geçiş

1. Testnet anahtarı oluşturun: https://testnet.binancefuture.com
2. `.env`: `TRADER_MODE=testnet`, `TRADER_API_KEY=...`, `TRADER_API_SECRET=...`
3. Testnette en az birkaç gün çalıştırıp dolum, SL/TP, acil stop ve teminat eklemeyi doğrulayın.
4. Canlıda yeni bir API anahtarı açın. **Para çekme yetkisi kapalı**, **IP kısıtlaması açık** olsun.
   Sonra `TRADER_MODE=live` yapın.

Canlı/testnet modunda:
- Bot, Binance'teki pozisyon ve bakiyeyi 5 saniyede bir kontrol eder. Binance'te kapanmış bir
  pozisyonu (acil stop, likidasyon, Binance uygulamasından elle kapatma) yerelde de kapatır.
- Hesap **one-way** pozisyon modunda olmalıdır (Binance varsayılanı).
- Acil durum stopu Binance'in Algo Order API'si (`/fapi/v1/algoOrder`) ile gönderilir; bu uç
  kullanılamazsa klasik `/fapi/v1/order` STOP_MARKET denenir.
- Canlı/testnet akışı sahte istemciyle test edildi, gerçek Binance'e karşı henüz denenmedi.
  İlk kullanım mutlaka testnette olmalı.
