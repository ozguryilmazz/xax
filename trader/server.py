"""FastAPI sunucusu. Arayüz tarayıcıda çalışır ve sunucuyla tek bir WebSocket üzerinden konuşur.

Sunucu her saniye bir "tick" mesajı gönderir: coin listesi, açık grafiklerin son mumları,
hesap/pozisyon/emir durumu ve yeni olaylar. Emir verme gibi işlemler de aynı WebSocket'ten gelir;
sunucu bunları Binance'e REST ile iletir (paper modda simüle eder).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .binance.rest import BinanceRest
from .config import Settings, get_settings
from .intervals import INTERVALS
from .market.binance_market import BinanceMarket
from .market.hub import MarketHub
from .market.sim_market import SimMarket
from .notify import Notifier
from .storage import Storage
from .trading.broker import PaperBroker
from .trading.live_broker import BinanceBroker
from .trading.manager import TradeError, TradeManager

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class Engine:
    def __init__(self, settings: Settings):
        self.s = settings
        self.rest = BinanceRest(settings.rest_base, settings.api_key, settings.api_secret.get_secret_value())
        if settings.market_source == "simulated":
            self.market: MarketHub = SimMarket(settings.top_n, settings.signal_min_volume_drop)
        else:
            # Piyasa verisi her zaman public uçlardan; testnet modunda testnet verisi
            self.market = BinanceMarket(self.rest, settings.ws_base, settings.top_n, settings.signal_min_volume_drop)
        broker = PaperBroker(settings.taker_fee) if settings.mode == "paper" else BinanceBroker(self.rest, settings.ws_base)
        self.storage = Storage(settings.db_path, settings.mode)
        self.notifier = Notifier(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id)
        self.manager = TradeManager(settings, self.market, broker, self.storage, self.notifier)

    async def start(self) -> None:
        self.notifier.start()
        await self.market.start()
        await self.manager.start()
        self.manager.event("info", f"Başlatıldı: mod={self.s.mode}, veri={self.s.market_source}", notify=True)

    async def stop(self) -> None:
        await self.manager.stop()
        await self.market.stop()
        await self.notifier.stop()
        await self.rest.close()


def candle_rows(candles) -> list[list]:
    return [[c.t, c.o, c.h, c.l, c.c, c.v, c.qv, int(c.closed)] for c in candles]


class ClientSession:
    def __init__(self, ws: WebSocket, engine: Engine):
        self.ws = ws
        self.engine = engine
        self.panes: dict[str, tuple[str, str]] = {}
        self.versions: dict[str, int] = {}
        self.last_event = 0
        self.trade_version = -1
        self._send_lock = asyncio.Lock()

    async def send(self, msg: dict) -> None:
        async with self._send_lock:
            await self.ws.send_json(msg)

    async def set_chart(self, pane: str, symbol: str, interval: str) -> None:
        if interval not in INTERVALS:
            raise TradeError(f"Geçersiz aralık: {interval}")
        market = self.engine.market
        if symbol not in market.tickers:
            raise TradeError(f"Bilinmeyen coin: {symbol}")
        old = self.panes.get(pane)
        if old == (symbol, interval):
            ser = market.series.get(old)
        else:
            ser = await market.acquire(symbol, interval)
            self.panes[pane] = (symbol, interval)
            if old:
                await market.release(*old)
        info = market.info(symbol)
        self.versions[pane] = ser.closed_version
        await self.send(
            {
                "type": "history", "pane": pane, "symbol": symbol, "interval": interval,
                "candles": candle_rows(ser.candles), "signals": ser.signals, "levels": ser.levels,
                "info": {"tick": info.tick_size, "step": info.step_size, "min_notional": info.min_notional},
            }
        )

    async def close(self) -> None:
        for symbol, interval in self.panes.values():
            await self.engine.market.release(symbol, interval)

    def tick_payload(self) -> dict:
        m = self.engine.manager
        market = self.engine.market
        charts = {}
        for pane, key in self.panes.items():
            ser = market.series.get(key)
            if not ser:
                continue
            c = {"symbol": key[0], "interval": key[1], "candles": candle_rows(ser.candles[-2:])}
            if ser.closed_version != self.versions.get(pane):
                self.versions[pane] = ser.closed_version
                c["signals"] = ser.signals
                c["levels"] = ser.levels
            charts[pane] = c
        events = [e for e in m.events if e["id"] > self.last_event]
        if events:
            self.last_event = events[-1]["id"]
        payload = {"type": "tick", "tickers": market.top_payload(), "charts": charts, "events": events,
                   "feed_age": market.feed_age(), **m.snapshot()}
        if self.trade_version != m.trade_version:
            self.trade_version = m.trade_version
            payload["trades"] = [t.to_dict() for t in m.storage.trades(100)]
            payload["stats"] = m.storage.stats()
        return payload

    async def handle(self, msg: dict) -> None:
        m = self.engine.manager
        kind = msg.get("type")
        req = msg.get("req")
        try:
            if kind == "chart":
                await self.set_chart(msg["pane"], msg["symbol"], msg["interval"])
                return
            if kind == "preview":
                data = m.preview(msg["symbol"], msg["side"], float(msg["price"]), float(msg["margin"]), int(msg["leverage"]))
            elif kind == "order":
                pane = msg.get("pane", "A")
                if pane not in self.panes:
                    raise TradeError(f"{pane} penceresinde açık grafik yok")
                symbol = self.panes[pane][0]
                o = await m.place_order(symbol, msg["side"], float(msg["price"]), float(msg["margin"]), int(msg["leverage"]), pane)
                data = {"id": o.id}
            elif kind == "cancel":
                await m.cancel_order(msg["id"])
                data = None
            elif kind == "add_margin":
                await m.add_margin(msg["symbol"], float(msg["amount"]))
                data = None
            elif kind == "close":
                await m.close_position(msg["symbol"], "MANUAL")
                data = None
            else:
                raise TradeError(f"Bilinmeyen istek: {kind}")
            await self.send({"type": "result", "req": req, "ok": True, "data": data})
        except (TradeError, KeyError, ValueError) as e:
            await self.send({"type": "result", "req": req, "ok": False, "error": str(e)})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = Engine(settings)
        await engine.start()
        app.state.engine = engine
        yield
        await engine.stop()

    app = FastAPI(title="Futures Trader", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    async def health():
        e: Engine = app.state.engine
        return {"mode": settings.mode, "source": settings.market_source, "symbols": len(e.market.top)}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        session = ClientSession(ws, app.state.engine)

        async def pusher():
            while True:
                await session.send(session.tick_payload())
                await asyncio.sleep(1)

        await session.send(
            {"type": "hello", "mode": settings.mode, "intervals": list(INTERVALS),
             "sl_pct": settings.sl_margin_pct, "tp_pct": settings.tp_margin_pct}
        )
        push_task = asyncio.create_task(pusher())
        try:
            while True:
                msg = await ws.receive_json()
                asyncio.create_task(session.handle(msg))
        except WebSocketDisconnect:
            pass
        finally:
            push_task.cancel()
            await session.close()

    return app
