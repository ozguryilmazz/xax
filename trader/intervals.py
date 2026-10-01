"""Grafik zaman aralıkları.

Binance'te 10 dakikalık mum yoktur; 1 dakikalık mumlardan yerelde üretilir.
"""
from __future__ import annotations

from dataclasses import dataclass

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR


@dataclass(frozen=True)
class Interval:
    name: str        # arayüzde gösterilen / kullanılan isim
    base: str        # Binance'ten çekilen aralık
    factor: int      # kaç base mum = 1 mum
    ms: int          # yaklaşık süre (1M için 30 gün)


INTERVALS: dict[str, Interval] = {
    i.name: i
    for i in [
        Interval("1m", "1m", 1, MIN),
        Interval("5m", "5m", 1, 5 * MIN),
        Interval("10m", "1m", 10, 10 * MIN),
        Interval("15m", "15m", 1, 15 * MIN),
        Interval("30m", "30m", 1, 30 * MIN),
        Interval("1h", "1h", 1, HOUR),
        Interval("2h", "2h", 1, 2 * HOUR),
        Interval("4h", "4h", 1, 4 * HOUR),
        Interval("6h", "6h", 1, 6 * HOUR),
        Interval("12h", "12h", 1, 12 * HOUR),
        Interval("1d", "1d", 1, DAY),
        Interval("1w", "1w", 1, 7 * DAY),
        Interval("1M", "1M", 1, 30 * DAY),
    ]
}


def get_interval(name: str) -> Interval:
    try:
        return INTERVALS[name]
    except KeyError:
        raise ValueError(f"Desteklenmeyen zaman aralığı: {name}") from None
