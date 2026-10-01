"""İnternet/Binance erişimi olmadan demo ve test için rastgele yürüyüş piyasası.

TRADER_MARKET_SOURCE=simulated ile açılır. Aynı MarketHub arayüzünü sağlar.
"""
from __future__ import annotations

import asyncio
import math
import random
import time

from ..intervals import INTERVALS
from ..models import Candle, SymbolInfo
from .hub import MarketHub

_BASES = {
    "BTCUSDT": (65000, 0.1, 0.001), "ETHUSDT": (3200, 0.01, 0.001), "SOLUSDT": (150, 0.01, 0.01),
    "BNBUSDT": (580, 0.01, 0.01), "XRPUSDT": (0.55, 0.0001, 0.1), "DOGEUSDT": (0.12, 0.00001, 1),
    "ADAUSDT": (0.45, 0.0001, 1), "AVAXUSDT": (28, 0.001, 1), "LINKUSDT": (14, 0.001, 0.01),
    "TRXUSDT": (0.12, 0.00001, 1), "DOTUSDT": (6, 0.001, 0.1), "LTCUSDT": (80, 0.01, 0.001),
}
_BASE_MS = {name: iv.ms for name, iv in INTERVALS.items() if iv.factor == 1}


class SimMarket(MarketHub):
    def __init__(self, top_n: int = 200, min_volume_drop: float = 0.0, seed: int = 7, extra_symbols: int = 30):
        super().__init__(top_n, min_volume_drop)
        self.rng = random.Random(seed)
        self.prices: dict[str, float] = {}
        self.open24: dict[str, float] = {}
        self.vol24: dict[str, float] = {}
        self._subs: set[tuple[str, str]] = set()
        self._live: dict[tuple[str, str], Candle] = {}
        self._task: asyncio.Task | None = None
        bases = dict(_BASES)
        for i in range(extra_symbols):
            bases[f"SIM{i:02d}USDT"] = (round(self.rng.uniform(0.05, 50), 3), 0.0001, 0.1)
        self._bases = bases

    async def start(self) -> None:
        for s, (p, tick, step) in self._bases.items():
            self.symbols[s] = SymbolInfo(s, tick_size=tick, step_size=step, min_qty=step)
            self.prices[s] = p
            self.open24[s] = p * self.rng.uniform(0.95, 1.05)
            self.vol24[s] = self.rng.uniform(5e6, 5e9) if s in _BASES else self.rng.uniform(1e5, 5e7)
            self._push(s)
        self.refresh_top()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _push(self, s: str) -> None:
        p = self.prices[s]
        self.update_ticker(s, p, (p - self.open24[s]) / self.open24[s] * 100, self.vol24[s])
        now = int(time.time() * 1000)
        self.update_mark(s, p, 0.0001, now - now % (8 * 3600_000) + 8 * 3600_000)

    def step(self) -> None:
        """Tüm fiyatları bir adım ilerletir (testlerde elle çağrılabilir)."""
        now = int(time.time() * 1000)
        for s in self.prices:
            p = self.prices[s] * math.exp(self.rng.gauss(0, 0.0012))
            tick = self.symbols[s].tick_size
            p = max(round(round(p / tick) * tick, 10), tick)
            self.prices[s] = p
            self.vol24[s] *= 1 + self.rng.uniform(-0.001, 0.0012)
            self._push(s)
        for s, base in list(self._subs):
            ms = _BASE_MS[base]
            t = now - now % ms
            p = self.prices[s]
            qv_add = self.vol24[s] / 86400 * self.rng.uniform(0.2, 3)
            cur = self._live.get((s, base))
            if cur and cur.t != t:
                cur.closed = True
                self.on_base_kline(s, base, cur)
                cur = None
            if cur is None:
                cur = Candle(t, p, p, p, p, 0, 0)
            cur.h, cur.l, cur.c = max(cur.h, p), min(cur.l, p), p
            cur.qv += qv_add
            cur.v += qv_add / p
            self._live[(s, base)] = cur
            self.on_base_kline(s, base, Candle(**cur.to_dict()))

    async def _run(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(0.5)
            self.step()
            n += 1
            if n % 60 == 0:
                self.refresh_top()

    async def fetch_history(self, symbol: str, base: str, count: int) -> list[Candle]:
        ms = _BASE_MS[base]
        now = int(time.time() * 1000)
        t_last = now - now % ms
        rng = random.Random(hash((symbol, base)) & 0xFFFF)
        p = self.prices[symbol]
        vol_scale = math.sqrt(ms / 60_000) * 0.002
        qv_base = self.vol24[symbol] / 86400 * ms / 1000
        rev: list[Candle] = []
        for i in range(count):
            t = t_last - i * ms
            c = p
            o = c * math.exp(-rng.gauss(0, vol_scale))
            h = max(o, c) * (1 + abs(rng.gauss(0, vol_scale / 2)))
            l = min(o, c) * (1 - abs(rng.gauss(0, vol_scale / 2)))
            qv = qv_base * rng.uniform(0.3, 2.0)
            rev.append(Candle(t, o, h, l, c, qv / c, qv, closed=i > 0))
            p = o
        out = list(reversed(rev))
        live = self._live.get((symbol, base))
        if live and live.t == out[-1].t:
            out[-1] = Candle(**live.to_dict())
        else:
            self._live[(symbol, base)] = Candle(**out[-1].to_dict())
        return out

    async def _subscribe_kline(self, symbol: str, base: str) -> None:
        self._subs.add((symbol, base))

    async def _unsubscribe_kline(self, symbol: str, base: str) -> None:
        self._subs.discard((symbol, base))
        self._live.pop((symbol, base), None)
