"""Ortak veri modelleri."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

Side = Literal["LONG", "SHORT"]


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def side_sign(side: Side) -> int:
    return 1 if side == "LONG" else -1


@dataclass
class Candle:
    t: int          # açılış zamanı (ms)
    o: float
    h: float
    l: float
    c: float
    v: float        # baz varlık hacmi
    qv: float       # USDT cinsinden hacim
    closed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SymbolInfo:
    symbol: str
    tick_size: float = 0.0001
    step_size: float = 0.001
    min_qty: float = 0.001
    min_notional: float = 5.0
    price_precision: int = 4
    qty_precision: int = 3


@dataclass
class Order:
    id: str
    symbol: str
    side: Side
    price: float
    qty: float
    leverage: int
    margin: float
    sl_price: float
    tp_price: float
    pane: str = "A"
    status: str = "NEW"          # NEW, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    created_at: int = field(default_factory=now_ms)
    exchange_id: Optional[str] = None

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled_qty, 0.0)

    @property
    def reserved_margin(self) -> float:
        """Henüz dolmamış kısım için ayrılan teminat."""
        return self.margin * (self.remaining / self.qty) if self.qty else 0.0


@dataclass
class Leg:
    """Bir pozisyona eklenmiş tek bir giriş. Her bacağın kendi değiştirilemez SL/TP'si vardır."""
    id: str
    order_id: str
    qty: float
    entry: float
    leverage: int
    margin: float
    sl_price: float
    tp_price: float
    entry_fee: float
    opened_at: int = field(default_factory=now_ms)


@dataclass
class Position:
    symbol: str
    side: Side
    legs: list[Leg] = field(default_factory=list)
    added_margin: float = 0.0     # likidasyonu uzaklaştırmak için sonradan eklenen teminat
    funding_paid: float = 0.0     # pozitif = ödenen (isolated teminattan düşer)
    liq_price: float = 0.0
    emergency_stop: Optional[float] = None
    emergency_order_id: Optional[str] = None
    opened_at: int = field(default_factory=now_ms)

    @property
    def qty(self) -> float:
        return sum(l.qty for l in self.legs)

    @property
    def entry(self) -> float:
        q = self.qty
        return sum(l.qty * l.entry for l in self.legs) / q if q else 0.0

    @property
    def notional(self) -> float:
        return self.qty * self.entry

    @property
    def leg_margin(self) -> float:
        return sum(l.margin for l in self.legs)

    @property
    def margin(self) -> float:
        """Isolated teminatın toplamı."""
        return self.leg_margin + self.added_margin - self.funding_paid

    @property
    def entry_fees(self) -> float:
        return sum(l.entry_fee for l in self.legs)

    @property
    def leverage(self) -> int:
        return self.legs[-1].leverage if self.legs else 1

    def unrealized(self, price: float) -> float:
        return side_sign(self.side) * (price - self.entry) * self.qty


@dataclass
class TradeRecord:
    id: str
    symbol: str
    side: Side
    qty: float
    entry: float
    exit: float
    pnl: float          # net: fiyat farkı - giriş/çıkış komisyonları - funding payı
    fee: float
    funding: float
    reason: str         # SL, TP, EMERGENCY, LIQUIDATION, MANUAL, EXTERNAL
    leverage: int
    margin: float
    opened_at: int
    closed_at: int = field(default_factory=now_ms)

    def to_dict(self) -> dict:
        return asdict(self)
