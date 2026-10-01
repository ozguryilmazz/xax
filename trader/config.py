"""Uygulama ayarları. Değerler `.env` dosyasından (TRADER_ önekiyle) okunur."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["paper", "testnet", "live"]

REST_URLS = {
    "live": "https://fapi.binance.com",
    "testnet": "https://testnet.binancefuture.com",
}
WS_URLS = {
    "live": "wss://fstream.binance.com",
    "testnet": "wss://stream.binancefuture.com",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TRADER_", extra="ignore")

    # paper: sanal bakiye, testnet: Binance Futures Testnet, live: gerçek hesap
    mode: Mode = "paper"
    # binance: gerçek piyasa verisi, simulated: internetsiz demo/test verisi
    market_source: Literal["binance", "simulated"] = "binance"

    api_key: str = ""
    api_secret: SecretStr = SecretStr("")
    # Boş bırakılırsa moda göre varsayılan kullanılır
    rest_url: str = ""
    ws_url: str = ""

    paper_start_balance: float = 1000.0
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005

    # Koruma seviyeleri teminatın yüzdesi olarak (0.30 = teminatın %30'u)
    sl_margin_pct: float = 0.30
    tp_margin_pct: float = 0.15

    # Risk limitleri
    max_daily_loss_usdt: float = 100.0
    max_open_positions: int = 5
    max_leverage: int = 20
    max_margin_per_order: float = 200.0

    top_n: int = 200
    # Sinyal: |fiyat değişimi| arttı ve hacim önceki muma göre en az bu oranda azaldı
    signal_min_volume_drop: float = 0.0

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    db_path: str = "data/trader.db"
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def exchange_env(self) -> Literal["live", "testnet"]:
        return "testnet" if self.mode == "testnet" else "live"

    @property
    def rest_base(self) -> str:
        return self.rest_url or REST_URLS[self.exchange_env]

    @property
    def ws_base(self) -> str:
        # Piyasa verisi paper modunda da gerçek (canlı) piyasadan gelir
        if self.ws_url:
            return self.ws_url
        return WS_URLS["testnet" if self.mode == "testnet" else "live"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
