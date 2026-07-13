"""Live spot-gold prices — keyless public sources, stdlib only.

Primary: Yahoo Finance chart API (XAUUSD=X spot rate, 1-minute bars).
Fallback: Stooq quote CSV. Both free, no API key. Degrades to None on
network failure — the paper loop just skips the tick.
"""
from __future__ import annotations

import json
import time
import urllib.request

UA = {"User-Agent": "goldtrader/1.0 (paper-trading research bot)"}
TIMEOUT = 12


def _get(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except Exception:
        return None


def yahoo_spot() -> float | None:
    raw = _get("https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X"
               "?interval=1m&range=1d")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        meta = data["chart"]["result"][0]["meta"]
        return float(meta["regularMarketPrice"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def stooq_spot() -> float | None:
    raw = _get("https://stooq.com/q/l/?s=xauusd&f=sd2t2ohlcv&h&e=csv")
    if not raw:
        return None
    try:
        lines = raw.decode().strip().splitlines()
        return float(lines[1].split(",")[6])   # close column
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


class GoldLiveFeed:
    def __init__(self):
        self.price_high = 0.0

    def spot(self) -> float | None:
        p = yahoo_spot() or stooq_spot()
        if p:
            self.price_high = max(self.price_high, p)
        return p
