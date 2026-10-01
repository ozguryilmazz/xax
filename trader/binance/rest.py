"""Binance USDⓈ-M Futures REST istemcisi (async).

Emirler REST üzerinden gider; piyasa verisi WebSocket'ten alınır (bkz. ws.py).
Ağırlık limiti aşımında (HTTP 429/418) istek atılmaz, hata yükseltilir.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from ..models import Candle, SymbolInfo

log = logging.getLogger(__name__)


class BinanceError(Exception):
    def __init__(self, status: int, code: int | None, msg: str):
        super().__init__(f"Binance hata {status}/{code}: {msg}")
        self.status, self.code, self.msg = status, code, msg


class BinanceRest:
    def __init__(self, base_url: str, api_key: str = "", api_secret: str = "", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._secret = api_secret.encode()
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        self._time_offset = 0
        self.used_weight = 0
        self._banned_until = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def has_keys(self) -> bool:
        return bool(self.api_key and self._secret)

    async def sync_time(self) -> None:
        data = await self._request("GET", "/fapi/v1/time")
        self._time_offset = int(data["serverTime"]) - int(time.time() * 1000)

    async def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False) -> Any:
        if time.time() < self._banned_until:
            raise BinanceError(429, None, "İstek limiti aşıldı, bekleniyor")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        if signed:
            if not self.has_keys:
                raise BinanceError(0, None, "API anahtarı tanımlı değil")
            params["timestamp"] = int(time.time() * 1000) + self._time_offset
            params["recvWindow"] = 5000
            query = urlencode(params)
            sig = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
            query += f"&signature={sig}"
            headers["X-MBX-APIKEY"] = self.api_key
        else:
            query = urlencode(params)
            if self.api_key:
                headers["X-MBX-APIKEY"] = self.api_key
        url = f"{path}?{query}" if query else path
        resp = await self._client.request(method, url, headers=headers)
        self.used_weight = int(resp.headers.get("X-MBX-USED-WEIGHT-1M", self.used_weight) or 0)
        if resp.status_code in (418, 429):
            retry = int(resp.headers.get("Retry-After", "60"))
            self._banned_until = time.time() + retry
            log.warning("Binance istek limiti: %s sn bekleniyor", retry)
        if resp.status_code >= 400:
            try:
                body = resp.json()
                raise BinanceError(resp.status_code, body.get("code"), body.get("msg", resp.text))
            except ValueError:
                raise BinanceError(resp.status_code, None, resp.text) from None
        return resp.json()

    # ---------- Piyasa (public) ----------
    async def exchange_info(self) -> dict[str, SymbolInfo]:
        data = await self._request("GET", "/fapi/v1/exchangeInfo")
        out: dict[str, SymbolInfo] = {}
        for s in data["symbols"]:
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING":
                continue
            info = SymbolInfo(s["symbol"], price_precision=s["pricePrecision"], qty_precision=s["quantityPrecision"])
            for f in s["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    info.tick_size = float(f["tickSize"])
                elif f["filterType"] == "LOT_SIZE":
                    info.step_size = float(f["stepSize"])
                    info.min_qty = float(f["minQty"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    info.min_notional = float(f["notional"])
            out[info.symbol] = info
        return out

    async def tickers_24h(self) -> list[dict]:
        return await self._request("GET", "/fapi/v1/ticker/24hr")

    async def klines(self, symbol: str, interval: str, limit: int = 1000, end_time: int | None = None) -> list[Candle]:
        rows = await self._request(
            "GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit, "endTime": end_time}
        )
        now = int(time.time() * 1000) + self._time_offset
        return [
            Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7]), int(r[6]) < now)
            for r in rows
        ]

    async def premium_index(self) -> list[dict]:
        return await self._request("GET", "/fapi/v1/premiumIndex")

    # ---------- Hesap / emir (signed) ----------
    async def leverage_brackets(self) -> list[dict]:
        return await self._request("GET", "/fapi/v1/leverageBracket", signed=True)

    async def balance(self) -> list[dict]:
        return await self._request("GET", "/fapi/v2/balance", signed=True)

    async def position_risk(self, symbol: str | None = None) -> list[dict]:
        return await self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> None:
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, signed=True)
        except BinanceError as e:
            if e.code != -4046:  # "No need to change margin type"
                raise

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    async def new_order(self, **params: Any) -> dict:
        return await self._request("POST", "/fapi/v1/order", params, signed=True)

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict:
        return await self._request(
            "DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True
        )

    async def new_stop_market_close(self, symbol: str, side: str, stop_price: str, client_id: str) -> str:
        """Pozisyonun tamamını kapatan STOP_MARKET (acil durum stopu).

        Binance koşullu emirleri Algo Order API'ye taşıdı; önce oradan denenir,
        desteklenmiyorsa klasik /fapi/v1/order kullanılır. Dönen değer iptal için gereken kimliktir.
        """
        try:
            r = await self._request(
                "POST",
                "/fapi/v1/algoOrder",
                {
                    "algoType": "CONDITIONAL",
                    "symbol": symbol,
                    "side": side,
                    "type": "STOP_MARKET",
                    "triggerPrice": stop_price,
                    "closePosition": "true",
                    "workingType": "MARK_PRICE",
                    "clientAlgoId": client_id,
                },
                signed=True,
            )
            return f"algo:{r['algoId']}"
        except BinanceError as e:
            if e.status not in (400, 404) or e.code in (-2019, -2021):
                raise
            log.info("Algo emir API kullanılamadı (%s), klasik emir deneniyor", e)
        await self.new_order(
            symbol=symbol, side=side, type="STOP_MARKET", stopPrice=stop_price, closePosition="true",
            workingType="MARK_PRICE", newClientOrderId=client_id,
        )
        return f"order:{client_id}"

    async def cancel_stop(self, symbol: str, ref: str) -> None:
        kind, _, ident = ref.partition(":")
        if kind == "algo":
            await self._request("DELETE", "/fapi/v1/algoOrder", {"algoId": ident}, signed=True)
        else:
            await self.cancel_order(symbol, ident)

    async def add_position_margin(self, symbol: str, amount: float) -> dict:
        return await self._request(
            "POST", "/fapi/v1/positionMargin", {"symbol": symbol, "amount": f"{amount:.4f}", "type": 1}, signed=True
        )

    async def new_listen_key(self) -> str:
        return (await self._request("POST", "/fapi/v1/listenKey", signed=False))["listenKey"]

    async def keepalive_listen_key(self) -> None:
        await self._request("PUT", "/fapi/v1/listenKey")
