"""Binance WebSocket bağlantıları.

MarketStream: tek bir birleşik bağlantı üzerinden
  - !ticker@arr          tüm coinlerin 24s istatistikleri (saniyede bir)
  - !markPrice@arr@1s    mark fiyatı + funding oranı (saniyede bir)
  - <symbol>@kline_<iv>  sadece açık grafikler için, dinamik SUBSCRIBE/UNSUBSCRIBE
Bağlantı koparsa üstel bekleme ile yeniden bağlanır ve tüm abonelikleri yeniler.

UserStream: listenKey ile hesap olayları (emir dolumları, bakiye/pozisyon güncellemeleri).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import websockets

log = logging.getLogger(__name__)

Handler = Callable[[str, dict | list], Awaitable[None] | None]


class MarketStream:
    def __init__(self, ws_base: str, handler: Handler, base_streams: list[str] | None = None):
        self.ws_base = ws_base.rstrip("/")
        self.handler = handler
        self.base_streams = base_streams or ["!ticker@arr", "!markPrice@arr@1s"]
        self.dynamic: set[str] = set()
        self._ws = None
        self._task: asyncio.Task | None = None
        self._msg_id = 0
        self.connected = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="market-stream")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._ws:
            await self._ws.close()

    async def subscribe(self, stream: str) -> None:
        if stream in self.dynamic:
            return
        self.dynamic.add(stream)
        await self._send("SUBSCRIBE", [stream])

    async def unsubscribe(self, stream: str) -> None:
        if stream not in self.dynamic:
            return
        self.dynamic.discard(stream)
        await self._send("UNSUBSCRIBE", [stream])

    async def _send(self, method: str, params: list[str]) -> None:
        if not self._ws or not self.connected.is_set():
            return  # yeniden bağlanınca tüm abonelikler zaten gönderilir
        self._msg_id += 1
        try:
            await self._ws.send(json.dumps({"method": method, "params": params, "id": self._msg_id}))
        except Exception as e:  # bağlantı kopmuş olabilir; _run yeniden bağlanır
            log.debug("ws gönderim hatası: %s", e)

    async def _run(self) -> None:
        delay = 1
        while True:
            url = f"{self.ws_base}/stream?streams={'/'.join(self.base_streams)}"
            try:
                async with websockets.connect(url, ping_interval=20, max_size=2**23) as ws:
                    self._ws = ws
                    self.connected.set()
                    delay = 1
                    log.info("Piyasa WebSocket bağlandı")
                    if self.dynamic:
                        await self._send("SUBSCRIBE", sorted(self.dynamic))
                    async for raw in ws:
                        msg = json.loads(raw)
                        stream = msg.get("stream")
                        if not stream:
                            continue
                        res = self.handler(stream, msg["data"])
                        if asyncio.iscoroutine(res):
                            await res
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Piyasa WebSocket koptu: %s (%s sn sonra tekrar)", e, delay)
            self.connected.clear()
            self._ws = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


class UserStream:
    def __init__(self, ws_base: str, rest, handler: Handler):
        self.ws_base = ws_base.rstrip("/")
        self.rest = rest
        self.handler = handler
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._run(), name="user-stream")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(30 * 60)
            try:
                await self.rest.keepalive_listen_key()
            except Exception as e:
                log.warning("listenKey yenilenemedi: %s", e)

    async def _run(self) -> None:
        delay = 1
        keepalive = asyncio.create_task(self._keepalive())
        try:
            while True:
                try:
                    key = await self.rest.new_listen_key()
                    async with websockets.connect(f"{self.ws_base}/ws/{key}", ping_interval=20) as ws:
                        delay = 1
                        log.info("Kullanıcı WebSocket bağlandı")
                        async for raw in ws:
                            data = json.loads(raw)
                            res = self.handler(data.get("e", ""), data)
                            if asyncio.iscoroutine(res):
                                await res
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("Kullanıcı WebSocket koptu: %s", e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        finally:
            keepalive.cancel()
