"""Borsa arayüzü. TradeManager tüm modlarda aynı kalır; sadece broker değişir.

PaperBroker: hiçbir şey göndermez, dolumları TradeManager fiyat akışından simüle eder.
BinanceBroker (live_broker.py): emirleri Binance REST API'ye gönderir.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..models import Order, Position, Side, SymbolInfo

if TYPE_CHECKING:
    from .manager import TradeManager


class Broker(ABC):
    simulated: bool = True

    async def start(self, manager: "TradeManager") -> None:
        self.manager = manager

    async def stop(self) -> None:
        pass

    @abstractmethod
    async def place_entry(self, order: Order, info: SymbolInfo) -> None:
        """Limit giriş emri. Reddedilirse istisna yükseltir."""

    @abstractmethod
    async def cancel_entry(self, order: Order) -> None: ...

    @abstractmethod
    async def close_qty(self, symbol: str, side: Side, qty: float, info: SymbolInfo, ref: str) -> tuple[float, float]:
        """Piyasa emriyle (reduce-only) kapatır. (ortalama fiyat, komisyon) döner."""

    @abstractmethod
    async def add_margin(self, symbol: str, amount: float) -> None: ...

    @abstractmethod
    async def set_emergency_stop(self, pos: Position, price: float, info: SymbolInfo) -> str | None: ...

    @abstractmethod
    async def cancel_emergency_stop(self, pos: Position) -> None: ...


class PaperBroker(Broker):
    simulated = True

    def __init__(self, taker_fee: float):
        self.taker_fee = taker_fee

    async def place_entry(self, order: Order, info: SymbolInfo) -> None:
        return None

    async def cancel_entry(self, order: Order) -> None:
        return None

    async def close_qty(self, symbol, side, qty, info, ref):
        price = self.manager.market.last_price(symbol)
        return price, price * qty * self.taker_fee

    async def add_margin(self, symbol: str, amount: float) -> None:
        return None

    async def set_emergency_stop(self, pos, price, info):
        return "paper"

    async def cancel_emergency_stop(self, pos) -> None:
        return None
