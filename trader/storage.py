"""SQLite kalıcı kayıt: program yeniden başlayınca açık emir/pozisyonlar ve SL/TP'ler geri yüklenir.

Her mod (paper / testnet / live) ayrı tutulur, birbirine karışmaz.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict

from .models import Leg, Order, Position, TradeRecord


class Storage:
    def __init__(self, path: str, mode: str):
        self.mode = mode
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (mode TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT, side TEXT, qty REAL, entry REAL,
                exit REAL, pnl REAL, fee REAL, funding REAL, reason TEXT, leverage INTEGER, margin REAL,
                opened_at INTEGER, closed_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS trades_mode_closed ON trades(mode, closed_at);
            CREATE TABLE IF NOT EXISTS events (ts INTEGER, mode TEXT, level TEXT, text TEXT);
            """
        )
        self.db.commit()

    def save_state(self, wallet: float, orders: dict[str, Order], positions: dict[str, Position]) -> None:
        data = {
            "wallet": wallet,
            "orders": [asdict(o) for o in orders.values()],
            "positions": [asdict(p) for p in positions.values()],
        }
        self.db.execute("INSERT OR REPLACE INTO state(mode, data) VALUES (?, ?)", (self.mode, json.dumps(data)))
        self.db.commit()

    def load_state(self) -> tuple[float | None, dict[str, Order], dict[str, Position]]:
        row = self.db.execute("SELECT data FROM state WHERE mode=?", (self.mode,)).fetchone()
        if not row:
            return None, {}, {}
        data = json.loads(row[0])
        orders = {o["id"]: Order(**o) for o in data.get("orders", [])}
        positions = {}
        for p in data.get("positions", []):
            legs = [Leg(**l) for l in p.pop("legs")]
            positions[p["symbol"]] = Position(legs=legs, **p)
        return data.get("wallet"), orders, positions

    def add_trade(self, t: TradeRecord) -> None:
        d = t.to_dict()
        cols = ",".join(d)
        self.db.execute(
            f"INSERT OR REPLACE INTO trades(mode,{cols}) VALUES (?,{','.join('?' * len(d))})",
            (self.mode, *d.values()),
        )
        self.db.commit()

    def trades(self, limit: int = 200) -> list[TradeRecord]:
        cur = self.db.execute(
            "SELECT id,symbol,side,qty,entry,exit,pnl,fee,funding,reason,leverage,margin,opened_at,closed_at "
            "FROM trades WHERE mode=? ORDER BY closed_at DESC LIMIT ?",
            (self.mode, limit),
        )
        return [TradeRecord(*r) for r in cur.fetchall()]

    def realized_since(self, ts_ms: int) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE mode=? AND closed_at>=?", (self.mode, ts_ms)
        ).fetchone()
        return float(row[0])

    def stats(self) -> dict:
        rows = self.db.execute(
            "SELECT pnl, fee, funding FROM trades WHERE mode=?", (self.mode,)
        ).fetchall()
        wins = [r[0] for r in rows if r[0] > 0]
        losses = [r[0] for r in rows if r[0] <= 0]
        n = len(rows)
        return {
            "count": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / n * 100 if n else 0.0,
            "net_pnl": sum(r[0] for r in rows),
            "fees": sum(r[1] for r in rows),
            "funding": sum(r[2] for r in rows),
            "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        }

    def log_event(self, ts: int, level: str, text: str) -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (ts, self.mode, level, text))
        self.db.commit()

    def reset(self) -> None:
        self.db.execute("DELETE FROM state WHERE mode=?", (self.mode,))
        self.db.execute("DELETE FROM trades WHERE mode=?", (self.mode,))
        self.db.commit()
