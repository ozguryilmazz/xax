"""Fiyat / hacim uyumsuzluğu sinyali.

Her kapanmış mum için:
  fiyat değişimi % = (kapanış - önceki kapanış) / önceki kapanış * 100
  hacim             = USDT cinsinden işlem hacmi

Sinyal: |fiyat değişimi| bir önceki muma göre arttı AMA USDT hacmi azaldı.
Yani hareket hızlanıyor ama hacimle desteklenmiyor. Mum yükselişse "up",
düşüşse "down" yönlü sinyal üretilir.

Sinyal olmasa da her mum için fiyat ve hacim değişimi istatistik olarak döndürülür
(arayüzde mumun üzerine gelince gösterilir).
"""
from __future__ import annotations

from ..models import Candle


def candle_stats(candles: list[Candle]) -> list[dict]:
    """Kapanmış mumlar için fiyat % ve hacim % değişimleri."""
    out: list[dict] = []
    prev: Candle | None = None
    for c in candles:
        if prev is None or prev.c == 0:
            pc = (c.c - c.o) / c.o * 100 if c.o else 0.0
            vc = None
        else:
            pc = (c.c - prev.c) / prev.c * 100
            vc = (c.qv - prev.qv) / prev.qv * 100 if prev.qv else None
        out.append({"t": c.t, "price_chg": pc, "qv": c.qv, "vol_chg": vc})
        prev = c
    return out


def divergence_signals(candles: list[Candle], min_volume_drop: float = 0.0) -> list[dict]:
    """Sadece kapanmış mumlar dikkate alınır.

    min_volume_drop: hacmin önceki muma göre en az ne kadar düşmesi gerektiği (0.05 = %5).
    """
    closed = [c for c in candles if c.closed]
    stats = candle_stats(closed)
    signals: list[dict] = []
    for i in range(2, len(closed)):
        cur, prev = stats[i], stats[i - 1]
        if abs(cur["price_chg"]) <= abs(prev["price_chg"]):
            continue
        if not closed[i - 1].qv or closed[i].qv >= closed[i - 1].qv * (1 - min_volume_drop):
            continue
        signals.append(
            {
                "t": cur["t"],
                "dir": "up" if cur["price_chg"] >= 0 else "down",
                "price_chg": cur["price_chg"],
                "prev_price_chg": prev["price_chg"],
                "qv": cur["qv"],
                "prev_qv": prev["qv"],
                "vol_chg": cur["vol_chg"],
            }
        )
    return signals
