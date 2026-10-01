"""İşlem yöneticisi: emirler, pozisyonlar, değiştirilemez SL/TP, acil durum stopu, risk limitleri.

Kurallar:
- Limit emir verildiği anda SL ve TP hesaplanır (SL = teminatın %30 kaybı, TP = %15 kazancı)
  ve bir daha değiştirilemez / iptal edilemez. Emir iptal edilirse sadece dolmamış kısım iptal olur.
- SL/TP Binance'e gönderilmez; bot tarafından fiyat akışı izlenerek tetiklenir.
- Binance'e sadece acil durum stopu gönderilir: en uzak SL ile likidasyon fiyatının tam ortası.
  Bot çalışmazken pozisyonu korur.
- Aynı coinde aynı yöne yeni emir mevcut pozisyona "bacak" olarak eklenir. Her bacağın kendi
  SL/TP'si vardır. Ters yöne emir reddedilir (Binance one-way modu pozisyonları netler).
- Teminat ekleme likidasyonu uzaklaştırır; SL/TP değişmez.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import deque
from typing import Optional

from ..config import Settings
from ..market.hub import MarketHub
from ..models import Leg, Order, Position, Side, TradeRecord, new_id, now_ms, side_sign
from ..notify import Notifier
from ..risk_math import (
    Bracket,
    breakeven_price,
    emergency_stop_price,
    liquidation_price,
    order_qty,
    protective_prices,
    round_step,
)
from ..storage import Storage
from .broker import Broker

log = logging.getLogger(__name__)


class TradeError(Exception):
    pass


def utc_day_start_ms() -> int:
    d = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


class TradeManager:
    def __init__(self, settings: Settings, market: MarketHub, broker: Broker, storage: Storage, notifier: Notifier | None = None):
        self.s = settings
        self.market = market
        self.broker = broker
        self.storage = storage
        self.notifier = notifier or Notifier()
        self.wallet = settings.paper_start_balance
        self.live_available: Optional[float] = None
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}
        self.brackets: dict[str, list[Bracket]] = {}
        self.events: deque[dict] = deque(maxlen=300)
        self.event_seq = 0
        self._closing: set[str] = set()
        self._lock = asyncio.Lock()
        self._emerg_locks: dict[str, asyncio.Lock] = {}
        self._emerg_placed: dict[str, float] = {}
        self._funding_next: dict[str, int] = {}
        self._funding_rate: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._daily_cache: tuple[int, float] | None = None
        self.trade_version = 0
        market.price_listeners.append(self.on_price)
        market.mark_listeners.append(self.on_mark)

    # ------------------------------------------------------------------ yaşam döngüsü
    async def start(self) -> None:
        wallet, orders, positions = self.storage.load_state()
        if wallet is not None:
            self.wallet = wallet
        self.orders, self.positions = orders, positions
        await self.broker.start(self)
        for pos in self.positions.values():
            self._update_risk(pos)
        if orders or positions:
            self.event("info", f"Kayıttan yüklendi: {len(positions)} pozisyon, {len(orders)} bekleyen emir")

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await self.broker.stop()
        self.persist()

    def persist(self) -> None:
        self.storage.save_state(self.wallet, self.orders, self.positions)

    def event(self, level: str, text: str, notify: bool = False) -> None:
        self.event_seq += 1
        e = {"id": self.event_seq, "ts": now_ms(), "level": level, "text": text}
        self.events.append(e)
        self.storage.log_event(e["ts"], level, text)
        log.info("[%s] %s", level, text)
        if notify:
            self.notifier.send(f"[{self.s.mode.upper()}] {text}")

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._task_done)

    def _task_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception():
            log.error("Arka plan görevi hata verdi", exc_info=t.exception())
            self.event("error", f"Hata: {t.exception()}", notify=True)

    # ------------------------------------------------------------------ hesap
    @property
    def used_margin(self) -> float:
        return sum(p.margin for p in self.positions.values()) + sum(o.reserved_margin for o in self.orders.values())

    @property
    def available(self) -> float:
        if self.live_available is not None:
            return self.live_available
        return self.wallet - self.used_margin

    def unrealized(self) -> float:
        total = 0.0
        for p in self.positions.values():
            m = self.market.mark_price(p.symbol)
            if m:
                total += p.unrealized(m)
        return total

    def daily_pnl(self) -> float:
        day = utc_day_start_ms()
        if not self._daily_cache or self._daily_cache[0] != day:
            self._daily_cache = (day, self.storage.realized_since(day))
        return self._daily_cache[1]

    # ------------------------------------------------------------------ emir verme
    def preview(self, symbol: str, side: Side, price: float, margin: float, leverage: int) -> dict:
        info = self.market.info(symbol)
        price = round_step(price, info.tick_size, "nearest")
        qty = order_qty(margin, leverage, price, info)
        if qty <= 0:
            raise TradeError("Miktar çok küçük; teminatı veya kaldıracı artırın")
        real_margin = qty * price / leverage
        sl, tp = protective_prices(side, price, qty, real_margin, self.s.sl_margin_pct, self.s.tp_margin_pct, info.tick_size)
        tmp = Position(symbol, side)
        existing = self.positions.get(symbol)
        if existing and existing.side == side:
            tmp = Position(symbol, side, legs=list(existing.legs), added_margin=existing.added_margin, funding_paid=existing.funding_paid)
        tmp.legs.append(Leg("preview", "preview", qty, price, leverage, real_margin, sl, tp, 0.0))
        tmp.liq_price = liquidation_price(side, tmp.qty, tmp.entry, tmp.margin, self.brackets.get(symbol))
        return {
            "symbol": symbol, "side": side, "price": price, "qty": qty, "notional": qty * price,
            "margin": real_margin, "leverage": leverage, "sl": sl, "tp": tp,
            "sl_loss": real_margin * self.s.sl_margin_pct, "tp_gain": real_margin * self.s.tp_margin_pct,
            "liq": tmp.liq_price, "emergency": emergency_stop_price(tmp, info.tick_size),
            "fee_est": qty * price * self.s.taker_fee,
        }

    def _check_risk(self, symbol: str, side: Side, margin: float, leverage: int, notional: float) -> None:
        s = self.s
        if not 1 <= leverage <= s.max_leverage:
            raise TradeError(f"Kaldıraç 1 ile {s.max_leverage} arasında olmalı")
        if margin <= 0 or margin > s.max_margin_per_order:
            raise TradeError(f"Emir başına teminat en fazla {s.max_margin_per_order} USDT olabilir")
        if margin + notional * s.taker_fee > self.available:
            raise TradeError(f"Yetersiz bakiye (kullanılabilir {self.available:.2f} USDT)")
        pos = self.positions.get(symbol)
        if (pos and pos.side != side) or any(o.symbol == symbol and o.side != side for o in self.orders.values()):
            raise TradeError("Bu coinde ters yönde pozisyon/emir var (one-way mod)")
        active = {p for p in self.positions} | {o.symbol for o in self.orders.values()}
        if symbol not in active and len(active) >= s.max_open_positions:
            raise TradeError(f"En fazla {s.max_open_positions} coinde aynı anda pozisyon açılabilir")
        if self.daily_pnl() <= -s.max_daily_loss_usdt:
            raise TradeError(f"Günlük zarar limiti ({s.max_daily_loss_usdt} USDT) doldu; yeni emir yarın açılır")

    async def place_order(self, symbol: str, side: Side, price: float, margin: float, leverage: int, pane: str = "A") -> Order:
        if side not in ("LONG", "SHORT"):
            raise TradeError("Yön LONG veya SHORT olmalı")
        if price <= 0:
            raise TradeError("Limit fiyatı geçersiz")
        if self.market.last_price(symbol) is None:
            raise TradeError(f"{symbol} için fiyat verisi yok")
        info = self.market.info(symbol)
        async with self._lock:
            p = self.preview(symbol, side, price, margin, leverage)
            if p["qty"] < info.min_qty or p["notional"] < info.min_notional:
                raise TradeError(f"Emir büyüklüğü en az {info.min_notional} USDT olmalı")
            self._check_risk(symbol, side, p["margin"], leverage, p["notional"])
            order = Order(
                id=new_id("en"), symbol=symbol, side=side, price=p["price"], qty=p["qty"], leverage=leverage,
                margin=p["margin"], sl_price=p["sl"], tp_price=p["tp"], pane=pane,
            )
            try:
                await self.broker.place_entry(order, info)
            except Exception as e:
                raise TradeError(f"Borsa emri reddetti: {e}") from e
            self.orders[order.id] = order
            self.persist()
        self.event(
            "info",
            f"{symbol} {side} limit {order.price} x {order.qty} ({leverage}x, teminat {order.margin:.2f}) "
            f"SL {order.sl_price} / TP {order.tp_price}",
        )
        if self.broker.simulated:
            last = self.market.last_price(symbol)
            if (side == "LONG" and last <= order.price) or (side == "SHORT" and last >= order.price):
                # Piyasa fiyatından daha iyi limit: hemen taker olarak dolar
                self.on_entry_fill(order.id, order.qty, last, order.qty * last * self.s.taker_fee)
        return order

    async def cancel_order(self, order_id: str) -> None:
        order = self.orders.get(order_id)
        if not order:
            raise TradeError("Emir bulunamadı")
        async with self._lock:
            await self.broker.cancel_entry(order)
            self.on_entry_cancelled(order_id)

    def on_entry_cancelled(self, order_id: str) -> None:
        order = self.orders.pop(order_id, None)
        if order:
            order.status = "CANCELED"
            self.persist()
            self.event("info", f"{order.symbol} emri iptal edildi (dolan: {order.filled_qty})")

    def on_entry_fill(self, order_id: str, qty: float, price: float, fee: float) -> None:
        order = self.orders.get(order_id)
        if not order or qty <= 0:
            return
        qty = min(qty, order.remaining)
        prev = order.filled_qty
        order.filled_qty += qty
        order.avg_fill_price = (order.avg_fill_price * prev + price * qty) / order.filled_qty
        order.status = "FILLED" if order.remaining <= 1e-12 else "PARTIALLY_FILLED"
        pos = self.positions.get(order.symbol)
        if pos is None:
            pos = Position(order.symbol, order.side)
            self.positions[order.symbol] = pos
        margin = qty * price / order.leverage
        leg = next((l for l in pos.legs if l.order_id == order.id), None)
        if leg:  # aynı emrin kısmi dolumları tek bacakta birleşir
            leg.entry = (leg.entry * leg.qty + price * qty) / (leg.qty + qty)
            leg.qty += qty
            leg.margin += margin
            leg.entry_fee += fee
        else:
            pos.legs.append(Leg(new_id("lg"), order.id, qty, price, order.leverage, margin, order.sl_price, order.tp_price, fee))
        if self.broker.simulated:
            self.wallet -= fee
        if order.status == "FILLED":
            del self.orders[order.id]
        self._update_risk(pos)
        self.persist()
        self.event(
            "success",
            f"{order.symbol} {order.side} doldu: {qty} @ {price:.8g} | pozisyon {pos.qty:.8g} @ {pos.entry:.8g}",
            notify=True,
        )

    # ------------------------------------------------------------------ risk seviyeleri
    def _update_risk(self, pos: Position, exchange_liq: float | None = None) -> None:
        info = self.market.info(pos.symbol)
        if exchange_liq is not None and exchange_liq > 0:
            pos.liq_price = exchange_liq
        else:
            pos.liq_price = liquidation_price(pos.side, pos.qty, pos.entry, pos.margin, self.brackets.get(pos.symbol))
        pos.emergency_stop = emergency_stop_price(pos, info.tick_size)
        if not self.broker.simulated:
            self._spawn(self._sync_emergency(pos.symbol))

    async def _sync_emergency(self, symbol: str) -> None:
        lock = self._emerg_locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            pos = self.positions.get(symbol)
            target = pos.emergency_stop if pos else None
            placed = self._emerg_placed.get(symbol)
            if target and placed and abs(target - placed) / placed < 0.001 and pos.emergency_order_id:
                return
            if pos and pos.emergency_order_id:
                try:
                    await self.broker.cancel_emergency_stop(pos)
                except Exception as e:
                    log.warning("Eski acil durum stopu iptal edilemedi: %s", e)
                pos.emergency_order_id = None
                self._emerg_placed.pop(symbol, None)
            if pos and target:
                ref = await self.broker.set_emergency_stop(pos, target, self.market.info(symbol))
                pos.emergency_order_id = ref
                self._emerg_placed[symbol] = target
                self.persist()

    def refresh_brackets(self, brackets: dict[str, list[Bracket]]) -> None:
        self.brackets = brackets
        for pos in self.positions.values():
            self._update_risk(pos)

    # ------------------------------------------------------------------ fiyat akışı
    def on_price(self, symbol: str, last: float) -> None:
        if self.broker.simulated:
            for order in [o for o in self.orders.values() if o.symbol == symbol]:
                if (order.side == "LONG" and last <= order.price) or (order.side == "SHORT" and last >= order.price):
                    self.on_entry_fill(order.id, order.remaining, order.price, order.remaining * order.price * self.s.maker_fee)
        pos = self.positions.get(symbol)
        if not pos:
            return
        for leg in list(pos.legs):
            if leg.id in self._closing:
                continue
            if pos.side == "LONG":
                reason = "SL" if last <= leg.sl_price else "TP" if last >= leg.tp_price else None
            else:
                reason = "SL" if last >= leg.sl_price else "TP" if last <= leg.tp_price else None
            if reason:
                self._closing.add(leg.id)
                self._spawn(self._close_leg(symbol, leg.id, reason))

    def on_mark(self, symbol: str, mark: float, rate: float, next_funding: int) -> None:
        pos = self.positions.get(symbol)
        prev_next = self._funding_next.get(symbol)
        if (
            self.broker.simulated and pos and prev_next and next_funding > prev_next
            and now_ms() >= prev_next - 1000
        ):
            self._apply_funding(pos, mark, self._funding_rate.get(symbol, rate))
        self._funding_next[symbol] = next_funding
        self._funding_rate[symbol] = rate
        if not pos or not self.broker.simulated or f"pos:{symbol}" in self._closing:
            return
        s = side_sign(pos.side)
        if pos.liq_price and (mark - pos.liq_price) * s <= 0:
            self._closing.add(f"pos:{symbol}")
            self._spawn(self._liquidate(symbol, pos.liq_price))
        elif pos.emergency_stop and (mark - pos.emergency_stop) * s <= 0:
            self._closing.add(f"pos:{symbol}")
            self._spawn(self.close_position(symbol, "EMERGENCY"))

    def _apply_funding(self, pos: Position, mark: float, rate: float) -> None:
        payment = side_sign(pos.side) * mark * pos.qty * rate
        self.on_funding(pos.symbol, payment)

    def on_funding(self, symbol: str, payment: float) -> None:
        """payment > 0: ödenen, < 0: alınan funding."""
        pos = self.positions.get(symbol)
        if not pos:
            return
        pos.funding_paid += payment
        if self.broker.simulated:
            self.wallet -= payment
        self._update_risk(pos)
        self.persist()
        self.event("info", f"{symbol} funding: {'-' if payment > 0 else '+'}{abs(payment):.4f} USDT")

    # ------------------------------------------------------------------ kapatma
    async def _close_leg(self, symbol: str, leg_id: str, reason: str) -> None:
        try:
            async with self._lock:
                pos = self.positions.get(symbol)
                leg = next((l for l in pos.legs if l.id == leg_id), None) if pos else None
                if not leg:
                    return
                price, fee = await self.broker.close_qty(symbol, pos.side, leg.qty, self.market.info(symbol), f"cl{leg.id}")
                self._book(pos, [leg], price, fee, reason)
        finally:
            self._closing.discard(leg_id)

    async def close_position(self, symbol: str, reason: str = "MANUAL") -> None:
        key = f"pos:{symbol}"
        self._closing.add(key)
        try:
            async with self._lock:
                pos = self.positions.get(symbol)
                if not pos:
                    return
                price, fee = await self.broker.close_qty(symbol, pos.side, pos.qty, self.market.info(symbol), f"cp{new_id('')}")
                self._book(pos, list(pos.legs), price, fee, reason)
        finally:
            self._closing.discard(key)

    def on_external_close(self, symbol: str, price: float, reason: str = "EXTERNAL") -> None:
        """Pozisyon borsada bot dışında kapandı (acil durum stopu, likidasyon, elle)."""
        pos = self.positions.get(symbol)
        if not pos:
            return
        self._book(pos, list(pos.legs), price, pos.qty * price * self.s.taker_fee, reason)

    async def _liquidate(self, symbol: str, price: float) -> None:
        try:
            async with self._lock:
                pos = self.positions.get(symbol)
                if not pos:
                    return
                total_q = pos.qty
                for leg in list(pos.legs):
                    share = leg.qty / total_q
                    loss = leg.margin + pos.added_margin * share
                    self._record(pos, leg, price, -(loss + leg.entry_fee), 0.0, pos.funding_paid * share, "LIQUIDATION", loss)
                if self.broker.simulated:
                    self.wallet -= pos.margin
                pos.legs.clear()
                self._finish_position(pos)
                self.event("error", f"{symbol} LİKİDE OLDU @ {price}", notify=True)
        finally:
            self._closing.discard(f"pos:{symbol}")

    def _book(self, pos: Position, legs: list[Leg], price: float, fee_total: float, reason: str) -> None:
        total_close = sum(l.qty for l in legs)
        s = side_sign(pos.side)
        net = 0.0
        for leg in legs:
            share = leg.qty / pos.qty
            added_share = pos.added_margin * share
            funding_share = pos.funding_paid * share
            fee = fee_total * leg.qty / total_close
            gross = s * (price - leg.entry) * leg.qty
            pnl = gross - leg.entry_fee - fee - funding_share
            if self.broker.simulated:
                self.wallet += gross - fee
            self._record(pos, leg, price, pnl, fee, funding_share, reason, leg.margin + added_share)
            pos.added_margin -= added_share
            pos.funding_paid -= funding_share
            pos.legs.remove(leg)
            net += pnl
        label = {"SL": "STOP LOSS", "TP": "TAKE PROFIT", "EMERGENCY": "ACİL DURUM STOPU", "MANUAL": "elle kapatıldı"}.get(reason, reason)
        self.event("success" if net > 0 else "warning", f"{pos.symbol} {label} @ {price:.8g} | net {net:+.2f} USDT", notify=True)
        if pos.legs:
            self._update_risk(pos)
        else:
            self._finish_position(pos)
        self.persist()

    def _record(self, pos: Position, leg: Leg, price: float, pnl: float, fee: float, funding: float, reason: str, margin: float) -> None:
        t = TradeRecord(
            id=new_id("tr"), symbol=pos.symbol, side=pos.side, qty=leg.qty, entry=leg.entry, exit=price,
            pnl=pnl, fee=fee + leg.entry_fee, funding=funding, reason=reason, leverage=leg.leverage,
            margin=margin, opened_at=leg.opened_at,
        )
        self.storage.add_trade(t)
        self._daily_cache = None
        self.trade_version += 1

    def _finish_position(self, pos: Position) -> None:
        self.positions.pop(pos.symbol, None)
        if pos.emergency_order_id and not self.broker.simulated:
            self._spawn(self._cancel_emergency_after_close(pos))
        self.persist()

    async def _cancel_emergency_after_close(self, pos: Position) -> None:
        try:
            await self.broker.cancel_emergency_stop(pos)
        except Exception as e:
            log.info("Acil durum stopu iptal edilemedi (zaten tetiklenmiş olabilir): %s", e)
        self._emerg_placed.pop(pos.symbol, None)

    # ------------------------------------------------------------------ teminat ekleme
    async def add_margin(self, symbol: str, amount: float) -> None:
        pos = self.positions.get(symbol)
        if not pos:
            raise TradeError("Pozisyon bulunamadı")
        if amount <= 0:
            raise TradeError("Tutar pozitif olmalı")
        if amount > self.available:
            raise TradeError(f"Yetersiz bakiye (kullanılabilir {self.available:.2f} USDT)")
        async with self._lock:
            await self.broker.add_margin(symbol, amount)
            pos.added_margin += amount
            old = pos.liq_price
            self._update_risk(pos)
            self.persist()
        self.event("info", f"{symbol} teminat +{amount:.2f} USDT, likidasyon {old:.6g} → {pos.liq_price:.6g}", notify=True)

    # ------------------------------------------------------------------ arayüz
    def snapshot(self) -> dict:
        positions = []
        for p in self.positions.values():
            mark = self.market.mark_price(p.symbol) or p.entry
            last = self.market.last_price(p.symbol) or mark
            upnl = p.unrealized(mark)
            positions.append(
                {
                    "symbol": p.symbol, "side": p.side, "qty": p.qty, "entry": p.entry, "mark": mark, "last": last,
                    "margin": p.margin, "added_margin": p.added_margin, "funding": p.funding_paid,
                    "leverage": p.leverage, "liq": p.liq_price, "emergency": p.emergency_stop,
                    "breakeven": breakeven_price(p, self.s.taker_fee), "upnl": upnl,
                    "roe": upnl / p.margin * 100 if p.margin else 0.0,
                    "legs": [
                        {"id": l.id, "qty": l.qty, "entry": l.entry, "leverage": l.leverage, "margin": l.margin,
                         "sl": l.sl_price, "tp": l.tp_price, "opened_at": l.opened_at}
                        for l in p.legs
                    ],
                }
            )
        orders = [
            {"id": o.id, "symbol": o.symbol, "side": o.side, "price": o.price, "qty": o.qty, "filled": o.filled_qty,
             "leverage": o.leverage, "margin": o.margin, "sl": o.sl_price, "tp": o.tp_price, "pane": o.pane,
             "status": o.status, "created_at": o.created_at}
            for o in self.orders.values()
        ]
        upnl = self.unrealized()
        return {
            "account": {
                "mode": self.s.mode, "wallet": self.wallet, "equity": self.wallet + upnl, "available": self.available,
                "used": self.used_margin, "upnl": upnl, "daily_pnl": self.daily_pnl(),
                "limits": {
                    "max_daily_loss": self.s.max_daily_loss_usdt, "max_open_positions": self.s.max_open_positions,
                    "max_leverage": self.s.max_leverage, "max_margin_per_order": self.s.max_margin_per_order,
                },
            },
            "positions": positions,
            "orders": orders,
        }
