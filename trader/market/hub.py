"""Piyasa verisi merkezi.

Kaynaktan (Binance veya simülasyon) gelen ticker / mark fiyatı / kline verilerini toplar,
en yüksek hacimli N coin listesini tutar ve açık grafikler için mum serilerini yönetir.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Callable

from ..intervals import get_interval
from ..models import Candle, SymbolInfo
from .candles import CandleSeries

log = logging.getLogger(__name__)

PriceListener = Callable[[str, float], None]
MarkListener = Callable[[str, float, float, int], None]


class MarketHub(ABC):
    def __init__(self, top_n: int = 200, min_volume_drop: float = 0.0):
        self.top_n = top_n
        self.min_volume_drop = min_volume_drop
        self.symbols: dict[str, SymbolInfo] = {}
        self.tickers: dict[str, dict] = {}
        self.marks: dict[str, dict] = {}
        self.top: list[str] = []
        self.series: dict[tuple[str, str], CandleSeries] = {}
        self._refs: dict[tuple[str, str], int] = {}
        self._base_subs: dict[tuple[str, str], int] = {}
        self.price_listeners: list[PriceListener] = []
        self.mark_listeners: list[MarkListener] = []
        self._closed_listeners: list[Callable[[CandleSeries], None]] = []

    # ---- kaynak tarafından uygulanır ----
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def fetch_history(self, symbol: str, base: str, count: int) -> list[Candle]: ...

    @abstractmethod
    async def _subscribe_kline(self, symbol: str, base: str) -> None: ...

    @abstractmethod
    async def _unsubscribe_kline(self, symbol: str, base: str) -> None: ...

    # ---- ortak ----
    def info(self, symbol: str) -> SymbolInfo:
        return self.symbols.get(symbol) or SymbolInfo(symbol)

    def last_price(self, symbol: str) -> float | None:
        t = self.tickers.get(symbol)
        return t["last"] if t else None

    def mark_price(self, symbol: str) -> float | None:
        m = self.marks.get(symbol)
        return m["mark"] if m else self.last_price(symbol)

    def update_ticker(self, symbol: str, last: float, chg_pct: float, qv: float) -> None:
        prev = self.tickers.get(symbol)
        direction = 0
        if prev:
            direction = 1 if last > prev["last"] else -1 if last < prev["last"] else prev["dir"]
        self.tickers[symbol] = {"last": last, "chg": chg_pct, "qv": qv, "dir": direction}
        for fn in self.price_listeners:
            fn(symbol, last)

    def update_mark(self, symbol: str, mark: float, funding_rate: float, next_funding: int) -> None:
        self.marks[symbol] = {"mark": mark, "rate": funding_rate, "next": next_funding}
        for fn in self.mark_listeners:
            fn(symbol, mark, funding_rate, next_funding)

    def refresh_top(self) -> None:
        eligible = [s for s in self.tickers if not self.symbols or s in self.symbols]
        self.top = sorted(eligible, key=lambda s: -self.tickers[s]["qv"])[: self.top_n]

    def top_payload(self) -> list[list]:
        return [
            [s, self.tickers[s]["last"], round(self.tickers[s]["chg"], 2), round(self.tickers[s]["qv"]), self.tickers[s]["dir"]]
            for s in self.top
            if s in self.tickers
        ]

    def on_candle_closed(self, fn: Callable[[CandleSeries], None]) -> None:
        self._closed_listeners.append(fn)

    def on_base_kline(self, symbol: str, base: str, c: Candle) -> None:
        for (sym, name), ser in self.series.items():
            if sym == symbol and ser.iv.base == base:
                if ser.on_base(c):
                    for fn in self._closed_listeners:
                        fn(ser)

    async def acquire(self, symbol: str, interval: str) -> CandleSeries:
        iv = get_interval(interval)
        key = (symbol, interval)
        self._refs[key] = self._refs.get(key, 0) + 1
        ser = self.series.get(key)
        if ser is None:
            ser = CandleSeries(symbol, iv, min_volume_drop=self.min_volume_drop)
            self.series[key] = ser
            bkey = (symbol, iv.base)
            self._base_subs[bkey] = self._base_subs.get(bkey, 0) + 1
            if self._base_subs[bkey] == 1:
                await self._subscribe_kline(symbol, iv.base)
            count = 1000 if iv.factor == 1 else 3000
            ser.load(await self.fetch_history(symbol, iv.base, count))
        elif not ser.ready:
            for _ in range(100):
                if ser.ready:
                    break
                await asyncio.sleep(0.05)
        return ser

    async def release(self, symbol: str, interval: str) -> None:
        key = (symbol, interval)
        if key not in self._refs:
            return
        self._refs[key] -= 1
        if self._refs[key] > 0:
            return
        del self._refs[key]
        ser = self.series.pop(key)
        bkey = (symbol, ser.iv.base)
        self._base_subs[bkey] -= 1
        if self._base_subs[bkey] == 0:
            del self._base_subs[bkey]
            await self._unsubscribe_kline(symbol, ser.iv.base)
