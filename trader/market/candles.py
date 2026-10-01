"""Mum serileri: geçmiş + canlı güncelleme, 10 dakika gibi türetilmiş aralıklar için birleştirme."""
from __future__ import annotations

from ..analysis.levels import support_resistance
from ..analysis.signals import divergence_signals
from ..intervals import Interval
from ..models import Candle


def _bucket(t: int, iv: Interval) -> int:
    return t - t % iv.ms


def _merge(parts: list[Candle], t: int, closed: bool) -> Candle:
    return Candle(
        t=t,
        o=parts[0].o,
        h=max(p.h for p in parts),
        l=min(p.l for p in parts),
        c=parts[-1].c,
        v=sum(p.v for p in parts),
        qv=sum(p.qv for p in parts),
        closed=closed,
    )


def aggregate(base: list[Candle], iv: Interval) -> list[Candle]:
    """1 dakikalık mumları iv aralığına (ör. 10m) birleştirir."""
    if iv.factor == 1:
        return list(base)
    base_ms = iv.ms // iv.factor
    groups: dict[int, list[Candle]] = {}
    for c in base:
        groups.setdefault(_bucket(c.t, iv), []).append(c)
    out = []
    for b in sorted(groups):
        parts = sorted(groups[b], key=lambda c: c.t)
        last = parts[-1]
        closed = last.closed and last.t == b + (iv.factor - 1) * base_ms
        out.append(_merge(parts, b, closed))
    # Baştaki eksik (ilk dakikaları olmayan) kova yanıltıcı olur, atılır
    if out and groups[out[0].t][0].t != out[0].t:
        out.pop(0)
    return out


class CandleSeries:
    def __init__(self, symbol: str, iv: Interval, max_len: int = 1500, min_volume_drop: float = 0.0):
        self.symbol = symbol
        self.iv = iv
        self.max_len = max_len
        self.min_volume_drop = min_volume_drop
        self.candles: list[Candle] = []
        self._parts: dict[int, Candle] = {}   # türetilmiş aralıkta mevcut kovanın base mumları
        self.closed_version = 0
        self._analysis_version = -1
        self._signals: list[dict] = []
        self._levels: list[dict] = []
        self.ready = False

    def load(self, base: list[Candle]) -> None:
        self.candles = aggregate(base, self.iv)[-self.max_len :]
        self._parts = {}
        if self.iv.factor > 1 and self.candles and not self.candles[-1].closed:
            b = self.candles[-1].t
            self._parts = {c.t: c for c in base if _bucket(c.t, self.iv) == b}
        self.closed_version += 1
        self.ready = True

    def on_base(self, c: Candle) -> bool:
        """Canlı base mum güncellemesi. Yeni bir mum kapandıysa True döner."""
        if not self.ready:
            return False
        if self.iv.factor == 1:
            return self._apply(c)
        b = _bucket(c.t, self.iv)
        if self.candles and b < self.candles[-1].t:
            return False
        if not self.candles or b > self.candles[-1].t:
            self._parts = {}
        self._parts[c.t] = c
        parts = sorted(self._parts.values(), key=lambda p: p.t)
        base_ms = self.iv.ms // self.iv.factor
        closed = c.closed and c.t == b + (self.iv.factor - 1) * base_ms
        return self._apply(_merge(parts, b, closed))

    def _apply(self, c: Candle) -> bool:
        closed_now = False
        if self.candles and c.t == self.candles[-1].t:
            was_closed = self.candles[-1].closed
            self.candles[-1] = c
            closed_now = c.closed and not was_closed
        elif not self.candles or c.t > self.candles[-1].t:
            if self.candles and not self.candles[-1].closed:
                self.candles[-1].closed = True
                closed_now = True
            self.candles.append(c)
            if c.closed:
                closed_now = True
            if len(self.candles) > self.max_len:
                del self.candles[: len(self.candles) - self.max_len]
        if closed_now:
            self.closed_version += 1
        return closed_now

    def _analyze(self) -> None:
        if self._analysis_version == self.closed_version:
            return
        self._signals = divergence_signals(self.candles, self.min_volume_drop)
        self._levels = support_resistance(self.candles)
        self._analysis_version = self.closed_version

    @property
    def signals(self) -> list[dict]:
        self._analyze()
        return self._signals

    @property
    def levels(self) -> list[dict]:
        self._analyze()
        return self._levels
