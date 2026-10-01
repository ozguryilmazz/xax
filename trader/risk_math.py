"""SL/TP, başa baş, likidasyon ve acil durum stopu hesapları."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from .models import Position, Side, SymbolInfo, side_sign

# Binance'in tipik USDT-M bakım teminatı kademeleri (notional üst sınırı, oran).
# Canlı/testnet modda API anahtarı varsa gerçek kademeler /fapi/v1/leverageBracket'ten alınır.
_DEFAULT_TIERS = [
    (50_000, 0.004),
    (250_000, 0.005),
    (3_000_000, 0.01),
    (15_000_000, 0.025),
    (30_000_000, 0.05),
    (80_000_000, 0.10),
    (float("inf"), 0.125),
]


@dataclass(frozen=True)
class Bracket:
    notional_cap: float
    mmr: float
    cum: float


def build_brackets(tiers: list[tuple[float, float]]) -> list[Bracket]:
    out: list[Bracket] = []
    cum = 0.0
    prev_cap, prev_mmr = 0.0, 0.0
    for cap, mmr in tiers:
        cum += prev_cap * (mmr - prev_mmr)
        out.append(Bracket(cap, mmr, cum))
        prev_cap, prev_mmr = cap, mmr
    return out


DEFAULT_BRACKETS = build_brackets(_DEFAULT_TIERS)


def bracket_for(notional: float, brackets: list[Bracket] | None = None) -> Bracket:
    for b in brackets or DEFAULT_BRACKETS:
        if notional <= b.notional_cap:
            return b
    return (brackets or DEFAULT_BRACKETS)[-1]


def _decimals(step: float) -> int:
    exp = Decimal(repr(step)).normalize().as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def round_step(value: float, step: float, mode: str = "down") -> float:
    """value'yu step katına yuvarlar. mode: down / up / nearest."""
    n = value / step
    if mode == "down":
        n = math.floor(n + 1e-9)
    elif mode == "up":
        n = math.ceil(n - 1e-9)
    else:
        n = round(n)
    return round(n * step, _decimals(step))


def order_qty(margin: float, leverage: int, price: float, info: SymbolInfo) -> float:
    return round_step(margin * leverage / price, info.step_size, "down")


def protective_prices(
    side: Side,
    entry: float,
    qty: float,
    margin: float,
    sl_pct: float,
    tp_pct: float,
    tick: float,
) -> tuple[float, float]:
    """SL = teminatın sl_pct kadarı kaybedildiği fiyat, TP = tp_pct kadarı kazanıldığı fiyat.

    Fiyat adımına yuvarlarken iki seviye de girişe doğru yuvarlanır; böylece SL'deki kayıp
    hiçbir zaman sl_pct'yi aşmaz.
    """
    s = side_sign(side)
    sl_dist = margin * sl_pct / qty
    tp_dist = margin * tp_pct / qty
    sl = entry - s * sl_dist
    tp = entry + s * tp_dist
    if side == "LONG":
        sl, tp = round_step(sl, tick, "up"), round_step(tp, tick, "down")
    else:
        sl, tp = round_step(sl, tick, "down"), round_step(tp, tick, "up")
    return max(sl, tick), tp


def liquidation_price(
    side: Side,
    qty: float,
    entry: float,
    isolated_margin: float,
    brackets: list[Bracket] | None = None,
) -> float:
    """Binance isolated / one-way modu likidasyon fiyatı formülü."""
    if qty <= 0:
        return 0.0
    b = bracket_for(qty * entry, brackets)
    s = side_sign(side)
    denom = qty * b.mmr - s * qty
    if denom == 0:
        return 0.0
    lp = (isolated_margin + b.cum - s * qty * entry) / denom
    return max(lp, 0.0)


def breakeven_price(pos: Position, taker_fee: float) -> float:
    """Ödenen giriş komisyonları, funding ve tahmini çıkış komisyonu dahil başa baş fiyatı."""
    q = pos.qty
    if q <= 0:
        return 0.0
    cost = (pos.entry_fees + pos.funding_paid) / q
    if pos.side == "LONG":
        return (pos.entry + cost) / (1 - taker_fee)
    return (pos.entry - cost) / (1 + taker_fee)


def emergency_stop_price(pos: Position, tick: float) -> float | None:
    """Acil durum stopu: en uzak bacak SL'si ile likidasyon fiyatının tam ortası.

    Bot çalışmazken Binance tarafında pozisyonu likidasyondan önce kapatır.
    """
    if not pos.legs or pos.liq_price <= 0:
        return None
    if pos.side == "LONG":
        worst_sl = min(l.sl_price for l in pos.legs)
        if worst_sl <= pos.liq_price:
            return None
        return round_step((worst_sl + pos.liq_price) / 2, tick, "up")
    worst_sl = max(l.sl_price for l in pos.legs)
    if worst_sl >= pos.liq_price:
        return None
    return round_step((worst_sl + pos.liq_price) / 2, tick, "down")
