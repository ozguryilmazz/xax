"""Telegram bildirimleri (token/chat id tanımlı değilse sessizce kapalıdır)."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.enabled = bool(token and chat_id)
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat = chat_id
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.enabled:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            log.warning("Bildirim kuyruğu dolu, mesaj atlandı")

    async def _run(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                text = await self._queue.get()
                try:
                    await client.post(self._url, json={"chat_id": self._chat, "text": text})
                except Exception as e:
                    log.warning("Telegram mesajı gönderilemedi: %s", e)
                await asyncio.sleep(0.05)
