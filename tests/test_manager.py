import asyncio

import pytest

from trader.config import Settings
from trader.market.hub import MarketHub
from trader.models import SymbolInfo
from trader.storage import Storage
from trader.trading.broker import PaperBroker
from trader.trading.manager import TradeError, TradeManager


class FakeMarket(MarketHub):
    async def start(self): ...
    async def stop(self): ...
    async def fetch_history(self, symbol, base, count): return []
    async def _subscribe_kline(self, symbol, base): ...
    async def _unsubscribe_kline(self, symbol, base): ...

    def px(self, symbol, price):
        self.update_mark(symbol, price, 0.0001, self.marks.get(symbol, {}).get("next", 10**15))
        self.update_ticker(symbol, price, 0, 1e9)


def settings(**kw):
    base = dict(mode="paper", paper_start_balance=1000, maker_fee=0.0002, taker_fee=0.0005,
                max_daily_loss_usdt=100, max_open_positions=2, max_leverage=20, max_margin_per_order=200)
    base.update(kw)
    return Settings(_env_file=None, **base)


async def make(storage=None, **kw):
    market = FakeMarket()
    market.symbols = {s: SymbolInfo(s, tick_size=0.01, step_size=0.001, min_notional=5) for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT")}
    for s in market.symbols:
        market.px(s, 100.0)
    s = settings(**kw)
    m = TradeManager(s, market, PaperBroker(s.taker_fee), storage or Storage(":memory:", "paper"))
    await m.start()
    return m, market


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_limit_order_fills_and_sl_closes_with_30pct_loss():
    m, mk = await make()
    o = await m.place_order("AAAUSDT", "LONG", 99.0, 10, 5)
    assert o.sl_price < 99 < o.tp_price
    assert "AAAUSDT" not in m.positions
    mk.px("AAAUSDT", 98.9)  # limit doldu (maker)
    pos = m.positions["AAAUSDT"]
    assert pos.qty == o.qty and pos.entry == 99.0
    mk.px("AAAUSDT", o.sl_price)
    await settle()
    assert "AAAUSDT" not in m.positions
    t = m.storage.trades()[0]
    assert t.reason == "SL"
    gross = (o.sl_price - 99.0) * o.qty
    assert gross == pytest.approx(-0.30 * o.margin, rel=1e-3)
    # cüzdan = başlangıç + net pnl
    assert m.wallet == pytest.approx(1000 + t.pnl)


async def test_tp_closes_with_15pct_gain():
    m, mk = await make()
    o = await m.place_order("AAAUSDT", "SHORT", 100.0, 20, 10)  # piyasada → hemen dolar
    assert "AAAUSDT" in m.positions
    mk.px("AAAUSDT", o.tp_price)
    await settle()
    t = m.storage.trades()[0]
    assert t.reason == "TP"
    assert (t.entry - t.exit) * t.qty == pytest.approx(0.15 * o.margin, rel=1e-3)


async def test_second_order_adds_leg_with_own_sl_tp_and_different_leverage():
    m, mk = await make()
    o1 = await m.place_order("AAAUSDT", "LONG", 100.0, 10, 5)
    mk.px("AAAUSDT", 102.0)
    o2 = await m.place_order("AAAUSDT", "LONG", 102.0, 20, 10)
    pos = m.positions["AAAUSDT"]
    assert len(pos.legs) == 2
    assert pos.legs[0].sl_price == o1.sl_price and pos.legs[1].sl_price == o2.sl_price
    assert pos.legs[1].leverage == 10
    assert pos.entry == pytest.approx((o1.qty * 100 + o2.qty * 102) / (o1.qty + o2.qty))
    # 2. bacağın SL'si (102 - 0.3*20/qty2) daha yakın → önce o kapanır, ilk bacak açık kalır
    mk.px("AAAUSDT", o2.sl_price)
    await settle()
    assert len(m.positions["AAAUSDT"].legs) == 1
    assert m.positions["AAAUSDT"].legs[0].order_id == o1.id


async def test_opposite_side_rejected():
    m, mk = await make()
    await m.place_order("AAAUSDT", "LONG", 100.0, 10, 5)
    with pytest.raises(TradeError, match="ters"):
        await m.place_order("AAAUSDT", "SHORT", 100.0, 10, 5)


async def test_risk_limits():
    m, mk = await make()
    with pytest.raises(TradeError, match="Kaldıraç"):
        await m.place_order("AAAUSDT", "LONG", 100.0, 10, 50)
    with pytest.raises(TradeError, match="teminat"):
        await m.place_order("AAAUSDT", "LONG", 100.0, 500, 5)
    await m.place_order("AAAUSDT", "LONG", 90.0, 10, 5)
    await m.place_order("BBBUSDT", "LONG", 90.0, 10, 5)
    with pytest.raises(TradeError, match="En fazla 2"):
        await m.place_order("CCCUSDT", "LONG", 90.0, 10, 5)


async def test_daily_loss_limit_blocks_new_orders():
    m, mk = await make(max_daily_loss_usdt=5)
    for _ in range(2):
        o = await m.place_order("AAAUSDT", "LONG", 100.0, 10, 5)
        mk.px("AAAUSDT", o.sl_price - 0.01)
        await settle()
        mk.px("AAAUSDT", 100.0)
    assert m.daily_pnl() < -5
    with pytest.raises(TradeError, match="Günlük zarar"):
        await m.place_order("AAAUSDT", "LONG", 100.0, 10, 5)


async def test_add_margin_moves_liq_but_not_sl_tp():
    m, mk = await make()
    await m.place_order("AAAUSDT", "LONG", 100.0, 10, 10)
    pos = m.positions["AAAUSDT"]
    liq0, sl0, tp0, em0 = pos.liq_price, pos.legs[0].sl_price, pos.legs[0].tp_price, pos.emergency_stop
    await m.add_margin("AAAUSDT", 10)
    assert pos.liq_price < liq0
    assert pos.emergency_stop < em0
    assert (pos.legs[0].sl_price, pos.legs[0].tp_price) == (sl0, tp0)
    assert sl0 > pos.emergency_stop > pos.liq_price


async def test_cancel_order_releases_margin():
    m, mk = await make()
    avail = m.available
    o = await m.place_order("AAAUSDT", "LONG", 90.0, 10, 5)
    assert m.available == pytest.approx(avail - o.margin)
    await m.cancel_order(o.id)
    assert m.available == pytest.approx(avail)


async def test_liquidation_in_paper():
    m, mk = await make()
    await m.place_order("AAAUSDT", "LONG", 100.0, 10, 20)
    pos = m.positions["AAAUSDT"]
    # SL'yi atlayan ani düşüş: sadece mark fiyatı güncellenir
    mk.update_mark("AAAUSDT", pos.liq_price - 0.5, 0.0001, 10**15)
    await settle()
    assert "AAAUSDT" not in m.positions
    assert m.storage.trades()[0].reason in ("LIQUIDATION", "EMERGENCY")


async def test_funding_applied_at_funding_time():
    m, mk = await make()
    await m.place_order("AAAUSDT", "LONG", 100.0, 10, 5)
    pos = m.positions["AAAUSDT"]
    mk.update_mark("AAAUSDT", 100.0, 0.001, 1000)
    mk.update_mark("AAAUSDT", 100.0, 0.001, 1000 + 8 * 3600_000)
    assert pos.funding_paid == pytest.approx(100.0 * pos.qty * 0.001)


async def test_state_persists_across_restart(tmp_path):
    db = str(tmp_path / "t.db")
    m, mk = await make(storage=Storage(db, "paper"))
    o = await m.place_order("AAAUSDT", "LONG", 100.0, 10, 5)
    await m.place_order("BBBUSDT", "SHORT", 110.0, 10, 5)
    wallet = m.wallet
    m2, _ = await make(storage=Storage(db, "paper"))
    assert m2.wallet == pytest.approx(wallet)
    assert m2.positions["AAAUSDT"].legs[0].sl_price == o.sl_price
    assert len(m2.orders) == 1


async def test_limit_price_far_from_market_rejected():
    m, mk = await make()
    # TRUMP 2.07'deyken MOVR fiyatıyla (2.84) emir: %37 uzak → reddedilir
    mk.px("AAAUSDT", 2.073)
    with pytest.raises(TradeError, match="çok uzak"):
        await m.place_order("AAAUSDT", "SHORT", 2.844, 10, 5)
    assert not m.orders
