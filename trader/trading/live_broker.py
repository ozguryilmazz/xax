"""Binance (canlı veya testnet) broker'ı.

- Giriş: isolated mod + kaldıraç ayarlanır, LIMIT GTC emir gönderilir.
- Dolumlar kullanıcı WebSocket akışından (ORDER_TRADE_UPDATE) gelir.
- SL/TP Binance'e GÖNDERİLMEZ; tetiklenince reduce-only MARKET emirle kapatılır.
- Sadece acil durum stopu (STOP_MARKET, closePosition) Binance'e gönderilir.
- 5 saniyede bir pozisyonlar ve bakiye REST ile uzlaştırılır; Binance'te kapanmış
  (acil stop/likidasyon/elle) pozisyonlar yerelde de kapatılır.
"""
from __future__ import annotations

import asyncio
import logging

from ..binance.rest import BinanceRest
from ..binance.ws import UserStream
from ..models import Order, Position, Side, SymbolInfo
from ..risk_math import Bracket, _decimals, build_brackets
from .broker import Broker

log = logging.getLogger(__name__)


def fmt(value: float, step: float) -> str:
    return f"{value:.{_decimals(step)}f}"


class BinanceBroker(Broker):
    simulated = False

    def __init__(self, rest: BinanceRest, ws_base: str):
        if not rest.has_keys:
            raise RuntimeError("Canlı/testnet mod için TRADER_API_KEY ve TRADER_API_SECRET gerekli")
        self.rest = rest
        self.user_stream = UserStream(ws_base, rest, self._on_user_event)
        self._task: asyncio.Task | None = None

    async def start(self, manager) -> None:
        await super().start(manager)
        await self.rest.sync_time()
        try:
            manager.refresh_brackets(await self._load_brackets())
        except Exception as e:
            log.warning("Kaldıraç kademeleri alınamadı, varsayılanlar kullanılacak: %s", e)
        await self._reconcile()
        self.user_stream.start()
        self._task = asyncio.create_task(self._reconcile_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.user_stream.stop()

    async def _load_brackets(self) -> dict[str, list[Bracket]]:
        out = {}
        for item in await self.rest.leverage_brackets():
            tiers = [(float(b["notionalCap"]), float(b["maintMarginRatio"])) for b in item["brackets"]]
            out[item["symbol"]] = build_brackets(tiers)
        return out

    # ---------------------------------------------------------------- emirler
    async def place_entry(self, order: Order, info: SymbolInfo) -> None:
        await self.rest.set_margin_type(order.symbol, "ISOLATED")
        await self.rest.set_leverage(order.symbol, order.leverage)
        r = await self.rest.new_order(
            symbol=order.symbol,
            side="BUY" if order.side == "LONG" else "SELL",
            type="LIMIT",
            timeInForce="GTC",
            price=fmt(order.price, info.tick_size),
            quantity=fmt(order.qty, info.step_size),
            newClientOrderId=order.id,
        )
        order.exchange_id = str(r.get("orderId"))

    async def cancel_entry(self, order: Order) -> None:
        await self.rest.cancel_order(order.symbol, order.id)

    async def close_qty(self, symbol: str, side: Side, qty: float, info: SymbolInfo, ref: str):
        r = await self.rest.new_order(
            symbol=symbol,
            side="SELL" if side == "LONG" else "BUY",
            type="MARKET",
            quantity=fmt(qty, info.step_size),
            reduceOnly="true",
            newClientOrderId=ref[:36],
            newOrderRespType="RESULT",
        )
        price = float(r.get("avgPrice") or 0) or self.manager.market.last_price(symbol)
        return price, price * qty * self.manager.s.taker_fee

    async def add_margin(self, symbol: str, amount: float) -> None:
        await self.rest.add_position_margin(symbol, amount)

    async def set_emergency_stop(self, pos: Position, price: float, info: SymbolInfo) -> str | None:
        side = "SELL" if pos.side == "LONG" else "BUY"
        return await self.rest.new_stop_market_close(pos.symbol, side, fmt(price, info.tick_size), f"em{pos.symbol[:20]}{int(price * 1e6) % 10**8}")

    async def cancel_emergency_stop(self, pos: Position) -> None:
        if pos.emergency_order_id and pos.emergency_order_id != "paper":
            await self.rest.cancel_stop(pos.symbol, pos.emergency_order_id)

    # ---------------------------------------------------------------- uzlaştırma
    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self._reconcile()
            except Exception as e:
                log.warning("Uzlaştırma hatası: %s", e)

    async def _reconcile(self) -> None:
        m = self.manager
        for b in await self.rest.balance():
            if b["asset"] == "USDT":
                m.wallet = float(b["balance"])
                m.live_available = float(b["availableBalance"])
        risk = {r["symbol"]: r for r in await self.rest.position_risk()}
        for symbol, pos in list(m.positions.items()):
            if f"pos:{symbol}" in m._closing or any(l.id in m._closing for l in pos.legs):
                continue
            r = risk.get(symbol)
            amt = abs(float(r["positionAmt"])) if r else 0.0
            if amt == 0:
                price = m.market.last_price(symbol) or pos.entry
                m.on_external_close(symbol, price, "EXTERNAL")
            else:
                liq = float(r.get("liquidationPrice") or 0)
                if abs(liq - pos.liq_price) > 1e-12:
                    m._update_risk(pos, exchange_liq=liq)

    def _on_user_event(self, event: str, data: dict) -> None:
        m = self.manager
        if event == "ORDER_TRADE_UPDATE":
            o = data["o"]
            cid = o.get("c", "")
            if cid not in m.orders:
                return
            if o.get("x") == "TRADE":
                m.on_entry_fill(cid, float(o["l"]), float(o["L"]), float(o.get("n") or 0))
            if o.get("X") in ("CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"):
                m.on_entry_cancelled(cid)
        elif event == "ACCOUNT_UPDATE":
            a = data["a"]
            if a.get("m") == "FUNDING_FEE":
                for b in a.get("B", []):
                    if b.get("a") != "USDT":
                        continue
                    for p in a.get("P", []):
                        pos = m.positions.get(p["s"])
                        if pos:
                            # isolated cüzdan (iw) farkı funding olarak yansıtılır
                            delta = pos.margin - float(p.get("iw") or pos.margin)
                            if abs(delta) > 1e-9:
                                m.on_funding(p["s"], delta)
