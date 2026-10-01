"""Başlatma: python run.py  →  tarayıcıda http://127.0.0.1:8000"""
import logging

import uvicorn

from trader.config import get_settings
from trader.server import create_app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level="info")
