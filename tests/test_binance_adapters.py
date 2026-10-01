"""Binance'e bağlanmadan, örnek mesajlarla veri ayrıştırma ve canlı broker akışı testleri."""
import asyncio

from trader.config import Settings
from trader.market.binance_market import BinanceMarket
from trader.models import SymbolInfo
from trader.storage import Storage
from trader.trading.live_broker import BinanceBroker
from trader.trading.manager import TradeManager


class FakeRest:
    has_keys = True

    def __init__(self):
        self.calls = []
        self.positions = {}

    async def sync_time(self): ...
    async def leverage_brackets(self): return [{"symbol": "BTCUSDT", "brackets": [{"notionalCap": 50000, "maintMarginRatio": 0.004}, {"notionalCap": 1e12, "maintMarginRatio": 0.01}]}]
    async def balance(self): return [{"asset": "USDT", "balance": "500", "availableBalance": "450"}]
    async def position_risk(self, symbol=None):
        return [{"symbol": s, "positionAmt": str(q), "liquidationPrice": "50000"} for s, q in self.positions.items()]
    async def set_margin_type(self, symbol, t="ISOLATED"): self.calls.append(("margin_type", symbol, t))
    async def set_leverage(self, symbol, lev): self.calls.append(("leverage", symbol, lev))
    async def new_order(self, **p):
        self.calls.append(("order", p))
        if p["type"] == "MARKET":
            return {"avgPrice": "61000", "orderId": 2}
        return {"orderId": 1}
    async def cancel_order(self, symbol, cid): self.calls.append(("cancel", cid))
    async def new_stop_market_close(self, symbol, side, price, cid):
        self.calls.append(("stop", side, price))
        return f"algo:{len(self.calls)}"
    async def cancel_stop(self, symbol, ref): self.calls.append(("cancel_stop", ref))
    async def add_position_margin(self, symbol, amount): self.calls.append(("add_margin", amount))
    async def new_listen_key(self): await asyncio.sleep(3600)
    async def keepalive_listen_key(self): ...
    async def exchange_info(self): return {}


def test_market_message_parsing():
    m = BinanceMarket(FakeRest(), "wss://x")
    m.symbols = {"BTCUSDT": SymbolInfo("BTCUSDT")}
    m._on_message("!ticker@arr", [{"s": "BTCUSDT", "c": "65000.5", "P": "-1.2", "q": "123456789"}, {"s": "ZZZ", "c": "1", "P": "0", "q": "1"}])
    assert m.tickers["BTCUSDT"]["last"] == 65000.5 and "ZZZ" not in m.tickers
    m._on_message("!markPrice@arr@1s", [{"s": "BTCUSDT", "p": "64990", "r": "0.0001", "T": 1700000000000}])
    assert m.marks["BTCUSDT"]["rate"] == 0.0001
    m.refresh_top()
    assert m.top == ["BTCUSDT"]


async def test_live_broker_flow():
    rest = FakeRest()
    s = Settings(_env_file=None, mode="testnet", max_margin_per_order=500)
    market = BinanceMarket(rest, "wss://x")
    market.symbols = {"BTCUSDT": SymbolInfo("BTCUSDT", tick_size=0.1, step_size=0.001)}
    market.update_ticker("BTCUSDT", 60000, 0, 1e9)
    broker = BinanceBroker(rest, "wss://x")
    m = TradeManager(s, market, broker, Storage(":memory:", "testnet"))
    await m.start()
    assert m.available == 450

    o = await m.place_order("BTCUSDT", "LONG", 60000, 100, 5)
    assert ("margin_type", "BTCUSDT", "ISOLATED") in rest.calls and ("leverage", "BTCUSDT", 5) in rest.calls
    entry = [c for c in rest.calls if c[0] == "order"][0][1]
    assert entry["type"] == "LIMIT" and entry["newClientOrderId"] == o.id and entry["price"] == "60000.0"
    # SL/TP Binance'e gönderilmedi
    assert not any(c[0] == "order" and c[1]["type"] != "LIMIT" for c in rest.calls)

    # Kullanıcı akışından dolum
    broker._on_user_event("ORDER_TRADE_UPDATE", {"o": {"c": o.id, "x": "TRADE", "X": "FILLED", "l": str(o.qty), "L": "60000", "n": "0.12"}})
    assert "BTCUSDT" in m.positions
    await asyncio.sleep(0.01)
    stops = [c for c in rest.calls if c[0] == "stop"]
    assert stops and stops[-1][1] == "SELL"
    pos = m.positions["BTCUSDT"]
    assert pos.legs[0].sl_price > float(stops[-1][2]) > pos.liq_price

    # SL tetiklenince reduce-only market emirle kapanır
    market.update_ticker("BTCUSDT", pos.legs[0].sl_price - 1, 0, 1e9)
    await asyncio.sleep(0.01)
    close = [c for c in rest.calls if c[0] == "order" and c[1]["type"] == "MARKET"]
    assert close and close[0][1]["reduceOnly"] == "true" and close[0][1]["side"] == "SELL"
    assert "BTCUSDT" not in m.positions
    await asyncio.sleep(0.01)
    assert any(c[0] == "cancel_stop" for c in rest.calls)
    await m.stop()


async def test_live_external_close_detected_by_reconcile():
    rest = FakeRest()
    s = Settings(_env_file=None, mode="testnet", max_margin_per_order=500)
    market = BinanceMarket(rest, "wss://x")
    market.symbols = {"BTCUSDT": SymbolInfo("BTCUSDT", tick_size=0.1, step_size=0.001)}
    market.update_ticker("BTCUSDT", 60000, 0, 1e9)
    broker = BinanceBroker(rest, "wss://x")
    m = TradeManager(s, market, broker, Storage(":memory:", "testnet"))
    await m.start()
    o = await m.place_order("BTCUSDT", "LONG", 60000, 100, 5)
    broker._on_user_event("ORDER_TRADE_UPDATE", {"o": {"c": o.id, "x": "TRADE", "X": "FILLED", "l": str(o.qty), "L": "60000", "n": "0"}})
    rest.positions = {"BTCUSDT": o.qty}
    await broker._reconcile()
    assert m.positions["BTCUSDT"].liq_price == 50000
    rest.positions = {"BTCUSDT": 0}
    await broker._reconcile()
    assert "BTCUSDT" not in m.positions
    assert m.storage.trades()[0].reason == "EXTERNAL"
    await m.stop()
