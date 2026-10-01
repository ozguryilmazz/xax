from trader.analysis.levels import support_resistance
from trader.analysis.signals import divergence_signals
from trader.intervals import get_interval
from trader.market.candles import CandleSeries, aggregate
from trader.models import Candle


def mk(t, o, c, qv, closed=True, h=None, l=None):
    return Candle(t, o, h if h is not None else max(o, c), l if l is not None else min(o, c), c, qv / c, qv, closed)


def test_signal_price_up_volume_down():
    candles = [mk(0, 100, 100, 1000), mk(1, 100, 101, 1000), mk(2, 101, 103, 800)]
    sigs = divergence_signals(candles)
    assert len(sigs) == 1
    assert sigs[0]["t"] == 2 and sigs[0]["dir"] == "up"


def test_no_signal_when_volume_rises_or_move_shrinks():
    assert divergence_signals([mk(0, 100, 100, 1000), mk(1, 100, 101, 1000), mk(2, 101, 103, 1200)]) == []
    assert divergence_signals([mk(0, 100, 100, 1000), mk(1, 100, 102, 1000), mk(2, 102, 102.5, 500)]) == []
    # Hacim değişmediyse sinyal yok
    assert divergence_signals([mk(0, 100, 100, 1000), mk(1, 100, 101, 1000), mk(2, 101, 103, 1000)]) == []


def test_signal_down_direction_and_open_candle_ignored():
    candles = [mk(0, 100, 100, 1000), mk(1, 100, 99, 1000), mk(2, 99, 96, 900)]
    assert divergence_signals(candles)[0]["dir"] == "down"
    candles[-1].closed = False
    assert divergence_signals(candles) == []


def test_aggregate_10m():
    iv = get_interval("10m")
    base = [mk(i * 60_000, 100 + i, 101 + i, 10) for i in range(20)]
    agg = aggregate(base, iv)
    assert len(agg) == 2
    assert agg[0].o == 100 and agg[0].c == 110 and agg[0].qv == 100 and agg[0].closed


def test_series_live_close_detection():
    ser = CandleSeries("X", get_interval("10m"))
    base = [mk(i * 60_000, 100, 100, 10) for i in range(15)]
    base[-1].closed = False
    ser.load(base)
    assert len(ser.candles) == 2 and not ser.candles[-1].closed
    closed_any = False
    for i in range(14, 20):
        closed_any |= ser.on_base(mk(i * 60_000, 100, 101, 10, closed=True))
    assert closed_any and ser.candles[-1].closed
    assert ser.on_base(mk(20 * 60_000, 101, 102, 10, closed=False)) is False
    assert len(ser.candles) == 3


def test_support_resistance_finds_repeated_levels():
    candles = []
    t = 0
    # 100 ile 110 arasında salınan fiyat: 110 direnç, 100 destek olmalı
    for cycle in range(6):
        for p in [102, 104, 106, 108, 110, 108, 106, 104, 102, 100]:
            candles.append(mk(t, p - 0.5, p, 1000, h=p + 0.2, l=p - 0.7))
            t += 1
    candles.append(mk(t, 104, 105, 1000))
    levels = support_resistance(candles, k=3)
    kinds = {l["kind"] for l in levels}
    assert kinds == {"support", "resistance"}
    res = max((l for l in levels if l["kind"] == "resistance"), key=lambda l: l["strength"])
    sup = max((l for l in levels if l["kind"] == "support"), key=lambda l: l["strength"])
    assert abs(res["price"] - 110.2) < 1
    assert abs(sup["price"] - 99.3) < 1
