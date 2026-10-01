"""MarketStream'in yeni Binance adresini (/market) kullandığını ve gerekirse eski adrese döndüğünü doğrular."""
import asyncio
import json
from http import HTTPStatus

import websockets

from trader.binance import ws as wsmod


async def _serve(allowed_prefix: str, seen: list[str]):
    def process_request(conn, request):
        seen.append(request.path)
        if not request.path.startswith(allowed_prefix):
            return conn.respond(HTTPStatus.NOT_FOUND, "yok\n")
        return None

    async def handler(conn):
        await conn.send(json.dumps({"stream": "!ticker@arr", "data": [{"s": "BTCUSDT", "c": "1", "P": "0", "q": "1"}]}))
        await asyncio.sleep(5)

    return await websockets.serve(handler, "127.0.0.1", 0, process_request=process_request)


async def _run(allowed_prefix: str):
    seen: list[str] = []
    server = await _serve(allowed_prefix, seen)
    port = server.sockets[0].getsockname()[1]
    got = asyncio.Event()
    stream = wsmod.MarketStream(f"ws://127.0.0.1:{port}", lambda s, d: got.set())
    stream.start()
    try:
        await asyncio.wait_for(got.wait(), timeout=5)
    finally:
        await stream.stop()
        server.close()
    return seen, stream


async def test_uses_new_market_path():
    seen, stream = await _run("/market/stream")
    assert seen[0].startswith("/market/stream?streams=!ticker@arr/!markPrice@arr@1s")
    assert stream.last_msg > 0


async def test_falls_back_to_legacy_path():
    seen, _ = await _run("/stream")
    assert seen[0].startswith("/market/stream") and seen[-1].startswith("/stream?")
