"""Güçlü destek / direnç seviyeleri.

Yöntem:
1. Pivot tepe/dipleri bul (her iki yanında k mum boyunca en yüksek/en düşük olan mumlar).
2. ATR'nin yarısı kadar yakın pivotları tek seviyede kümele.
3. Her seviyeye güç puanı ver:
     - pivot sayısı (seviyenin kaç kez test edildiği)
     - fitili seviyeye değip gövdesi geçemeyen mumlar (reddedilmeler)
     - pivot mumlarının hacmi (ortalama hacme göre)
     - yakınlık (yeni pivotlar daha ağırlıklı)
4. Son fiyatın üstündekiler direnç, altındakiler destek. Her yandan en güçlü N seviye döner.
"""
from __future__ import annotations

import math

from ..models import Candle


def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for prev, c in zip(candles, candles[1:]):
        trs.append(max(c.h - c.l, abs(c.h - prev.c), abs(c.l - prev.c)))
    tail = trs[-period:]
    return sum(tail) / len(tail)


def _pivots(candles: list[Candle], k: int) -> list[tuple[int, float, str]]:
    out = []
    for i in range(k, len(candles) - k):
        window = candles[i - k : i + k + 1]
        c = candles[i]
        if c.h >= max(w.h for w in window):
            out.append((i, c.h, "H"))
        if c.l <= min(w.l for w in window):
            out.append((i, c.l, "L"))
    return out


def support_resistance(
    candles: list[Candle],
    k: int = 5,
    per_side: int = 3,
    min_touches: int = 2,
    lookback: int = 500,
) -> list[dict]:
    candles = [c for c in candles if c.closed][-lookback:]
    if len(candles) < 2 * k + 3:
        return []
    last = candles[-1].c
    tol = max(atr(candles) * 0.5, last * 0.001)
    avg_qv = sum(c.qv for c in candles) / len(candles) or 1.0
    n = len(candles)

    pivots = sorted(_pivots(candles, k), key=lambda p: p[1])
    clusters: list[list[tuple[int, float, str]]] = []
    for p in pivots:
        if clusters:
            mean = sum(x[1] for x in clusters[-1]) / len(clusters[-1])
            if abs(p[1] - mean) <= tol:
                clusters[-1].append(p)
                continue
        clusters.append([p])

    levels = []
    for cl in clusters:
        if len(cl) < min_touches:
            continue
        price = sum(x[1] for x in cl) / len(cl)
        rejections = 0
        for c in candles:
            body_hi, body_lo = max(c.o, c.c), min(c.o, c.c)
            if c.h >= price - tol and body_hi < price - tol * 0.2 and c.h <= price + tol:
                rejections += 1
            elif c.l <= price + tol and body_lo > price + tol * 0.2 and c.l >= price - tol:
                rejections += 1
        vol = sum(candles[i].qv for i, _, _ in cl) / len(cl) / avg_qv
        recency = sum(math.exp(-(n - 1 - i) / (n / 2)) for i, _, _ in cl)
        score = len(cl) * 2.0 + rejections * 0.5 + vol + recency
        levels.append({"price": price, "touches": len(cl), "score": score})

    if not levels:
        return []
    top = max(l["score"] for l in levels)
    for l in levels:
        l["strength"] = round(l["score"] / top, 3)
        l["kind"] = "resistance" if l["price"] > last else "support"

    res = sorted((l for l in levels if l["kind"] == "resistance"), key=lambda l: -l["score"])[:per_side]
    sup = sorted((l for l in levels if l["kind"] == "support"), key=lambda l: -l["score"])[:per_side]
    return res + sup
