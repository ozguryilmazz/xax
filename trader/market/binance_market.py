"""Binance USDⓈ-M Futures piyasa verisi kaynağı (WebSocket ağırlıklı)."""
from __future__ import annotations

import asyncio
import logging

from ..binance.rest import BinanceRest
from ..binance.ws import MarketStream
from ..models import Candle
from .hub import MarketHub

log = logging.getLogger(__name__)


class BinanceMarket(MarketHub):
    def __init__(self, rest: BinanceRest, ws_base: str, top_n: int = 200, min_volume_drop: float = 0.0):
        super().__init__(top_n, min_volume_drop)
        self.rest = rest
        self.stream = MarketStream(ws_base, self._on_message)
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self.symbols = await self.rest.exchange_info()
        # İlk doldurma REST ile (tek seferlik), sonrası tamamen WebSocket
        for t in await self.rest.tickers_24h():
            if t["symbol"] in self.symbols:
                self.update_ticker(t["symbol"], float(t["lastPrice"]), float(t["priceChangePercent"]), float(t["quoteVolume"]))
        for p in await self.rest.premium_index():
            if p["symbol"] in self.symbols:
                self.update_mark(p["symbol"], float(p["markPrice"]), float(p["lastFundingRate"] or 0), int(p["nextFundingTime"]))
        self.refresh_top()
        self.stream.start()
        self._tasks.append(asyncio.create_task(self._periodic()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.stream.stop()

    async def _periodic(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(30)
            self.refresh_top()
            n += 1
            if n % 120 == 0:  # saatte bir yeni listelenen/kaldırılan coinler
                try:
                    self.symbols = await self.rest.exchange_info()
                except Exception as e:
                    log.warning("exchangeInfo yenilenemedi: %s", e)

    def _on_message(self, stream: str, data) -> None:
        if stream == "!ticker@arr":
            for t in data:
                s = t["s"]
                if s in self.symbols:
                    self.update_ticker(s, float(t["c"]), float(t["P"]), float(t["q"]))
        elif stream.startswith("!markPrice@arr"):
            for m in data:
                s = m["s"]
                if s in self.symbols:
                    self.update_mark(s, float(m["p"]), float(m.get("r") or 0), int(m.get("T") or 0))
        elif "@kline_" in stream:
            k = data["k"]
            c = Candle(int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"]), float(k["q"]), bool(k["x"]))
            self.on_base_kline(k["s"], k["i"], c)

    async def fetch_history(self, symbol: str, base: str, count: int) -> list[Candle]:
        out: list[Candle] = []
        end = None
        while len(out) < count:
            want = min(1500, count - len(out))
            batch = await self.rest.klines(symbol, base, limit=want, end_time=end)
            out = batch + out
            if len(batch) < want:
                break
            end = batch[0].t - 1
        return out

    async def _subscribe_kline(self, symbol: str, base: str) -> None:
        await self.stream.subscribe(f"{symbol.lower()}@kline_{base}")

    async def _unsubscribe_kline(self, symbol: str, base: str) -> None:
        await self.stream.unsubscribe(f"{symbol.lower()}@kline_{base}")
