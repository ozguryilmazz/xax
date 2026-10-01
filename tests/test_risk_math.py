import pytest

from trader.models import Leg, Position, SymbolInfo
from trader.risk_math import (
    breakeven_price,
    emergency_stop_price,
    liquidation_price,
    order_qty,
    protective_prices,
    round_step,
)


def test_round_step():
    assert round_step(1.23456, 0.01, "down") == 1.23
    assert round_step(1.23456, 0.01, "up") == 1.24
    assert round_step(0.000123456, 0.00001, "nearest") == 0.00012
    assert round_step(1.2, 0.1, "down") == 1.2  # kayan nokta hatasıyla 1.1 olmamalı
    assert round_step(0.3, 0.00025, "down") == 0.3


def test_sl_tp_example_10usdt_5x():
    # 10 USDT teminat, 5x, giriş 100 → 0.5 adet, pozisyon 50 USDT
    info = SymbolInfo("X", tick_size=0.01, step_size=0.001)
    qty = order_qty(10, 5, 100, info)
    assert qty == 0.5
    sl, tp = protective_prices("LONG", 100, qty, 10, 0.30, 0.15, 0.01)
    # SL'de kayıp 3 USDT (%30), TP'de kazanç 1.5 USDT (%15)
    assert sl == 94.0 and tp == 103.0
    assert (100 - sl) * qty == pytest.approx(3.0)
    assert (tp - 100) * qty == pytest.approx(1.5)

    sl, tp = protective_prices("SHORT", 100, qty, 10, 0.30, 0.15, 0.01)
    assert sl == 106.0 and tp == 97.0


def test_sl_rounding_never_exceeds_risk():
    sl, _ = protective_prices("LONG", 1.2345, 81.0, 10, 0.30, 0.15, 0.001)
    assert (1.2345 - sl) * 81.0 <= 3.0 + 1e-9


def test_liquidation_long_short():
    # 5x isolated, MMR %0.4: long likidasyon ≈ entry * (1 - 1/5) / (1 - 0.004)
    lp = liquidation_price("LONG", 0.5, 100, 10)
    assert lp == pytest.approx(100 * 0.8 / 0.996)
    sp = liquidation_price("SHORT", 0.5, 100, 10)
    assert sp == pytest.approx(100 * 1.2 / 1.004)


def test_more_margin_moves_liquidation_away():
    assert liquidation_price("LONG", 0.5, 100, 15) < liquidation_price("LONG", 0.5, 100, 10)
    assert liquidation_price("SHORT", 0.5, 100, 15) > liquidation_price("SHORT", 0.5, 100, 10)


def test_emergency_stop_is_midpoint_between_sl_and_liq():
    pos = Position("X", "LONG", legs=[Leg("l", "o", 0.5, 100, 5, 10, 94, 103, 0)])
    pos.liq_price = 80.0
    assert emergency_stop_price(pos, 0.01) == 87.0
    pos = Position("X", "SHORT", legs=[Leg("l", "o", 0.5, 100, 5, 10, 106, 97, 0)])
    pos.liq_price = 120.0
    assert emergency_stop_price(pos, 0.01) == 113.0


def test_breakeven_includes_fees():
    pos = Position("X", "LONG", legs=[Leg("l", "o", 1, 100, 5, 20, 70, 115, 0.05)])
    be = breakeven_price(pos, 0.0005)
    # be fiyatında kapatınca: (be-100) - be*0.0005 - 0.05 = 0
    assert (be - 100) - be * 0.0005 - 0.05 == pytest.approx(0)
