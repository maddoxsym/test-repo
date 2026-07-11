"""
Sqlite trade journal.  Every trade the bot opens is recorded with the full
context of WHY it was opened (which analysts confirmed, regime, score), and
updated with the outcome when it closes.  The learning engine feeds on this.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_trade_id TEXT UNIQUE,
    instrument      TEXT NOT NULL,
    direction       INTEGER NOT NULL,          -- +1 long, -1 short
    units           REAL NOT NULL,
    entry_price     REAL NOT NULL,
    stop_price      REAL NOT NULL,
    tp_price        REAL NOT NULL,
    risk_usd        REAL NOT NULL,
    atr             REAL,
    regime          TEXT,
    confirmations   TEXT,                      -- json list of strategy names
    council_score   REAL,
    opened_at       REAL NOT NULL,             -- unix epoch UTC
    closed_at       REAL,
    close_price     REAL,
    pnl             REAL,
    r_multiple      REAL,
    status          TEXT NOT NULL DEFAULT 'open',   -- open | closed
    paper           INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS strategy_weights (
    name       TEXT PRIMARY KEY,
    weight     REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS combo_stats (
    combo    TEXT NOT NULL,                    -- sorted strategy names, '+'-joined
    regime   TEXT NOT NULL,
    trades   INTEGER NOT NULL DEFAULT 0,
    wins     INTEGER NOT NULL DEFAULT 0,
    total_r  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (combo, regime)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class TradeRecord:
    instrument: str
    direction: int
    units: float
    entry_price: float
    stop_price: float
    tp_price: float
    risk_usd: float
    atr: Optional[float] = None
    regime: str = ""
    confirmations: list[str] = field(default_factory=list)
    council_score: float = 0.0
    opened_at: float = field(default_factory=time.time)
    broker_trade_id: Optional[str] = None
    paper: bool = False
    id: Optional[int] = None
    status: str = "open"
    closed_at: Optional[float] = None
    close_price: Optional[float] = None
    pnl: Optional[float] = None
    r_multiple: Optional[float] = None


def _row_to_record(row: sqlite3.Row) -> TradeRecord:
    return TradeRecord(
        id=row["id"], broker_trade_id=row["broker_trade_id"],
        instrument=row["instrument"], direction=row["direction"],
        units=row["units"], entry_price=row["entry_price"],
        stop_price=row["stop_price"], tp_price=row["tp_price"],
        risk_usd=row["risk_usd"], atr=row["atr"], regime=row["regime"] or "",
        confirmations=json.loads(row["confirmations"] or "[]"),
        council_score=row["council_score"] or 0.0,
        opened_at=row["opened_at"], closed_at=row["closed_at"],
        close_price=row["close_price"], pnl=row["pnl"],
        r_multiple=row["r_multiple"], status=row["status"],
        paper=bool(row["paper"]),
    )


class Journal:
    def __init__(self, db_path: str = "bot_data.sqlite3"):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock, self.conn:
            self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------- trades

    def record_open(self, t: TradeRecord) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO trades (broker_trade_id, instrument, direction,
                       units, entry_price, stop_price, tp_price, risk_usd, atr,
                       regime, confirmations, council_score, opened_at, status, paper)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?)""",
                (t.broker_trade_id, t.instrument, t.direction, t.units,
                 t.entry_price, t.stop_price, t.tp_price, t.risk_usd, t.atr,
                 t.regime, json.dumps(sorted(t.confirmations)), t.council_score,
                 t.opened_at, int(t.paper)))
            t.id = cur.lastrowid
            return t.id

    def record_close(self, trade_id: int, close_price: float, pnl: float,
                     closed_at: Optional[float] = None) -> Optional[TradeRecord]:
        closed_at = closed_at or time.time()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
            if row is None:
                return None
            risk = row["risk_usd"] or 0.0
            r_mult = (pnl / risk) if risk > 0 else 0.0
            self.conn.execute(
                """UPDATE trades SET status='closed', closed_at=?, close_price=?,
                       pnl=?, r_multiple=? WHERE id=?""",
                (closed_at, close_price, pnl, r_mult, trade_id))
            row = self.conn.execute(
                "SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
            return _row_to_record(row)

    def open_trades(self) -> list[TradeRecord]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE status='open'").fetchall()
        return [_row_to_record(r) for r in rows]

    def recent_closed(self, limit: int = 50) -> list[TradeRecord]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM trades WHERE status='closed'
                   ORDER BY closed_at DESC LIMIT ?""", (limit,)).fetchall()
        return [_row_to_record(r) for r in rows]

    # -------------------------------------------------------- daily stats

    @staticmethod
    def _day_bounds_utc(now: Optional[float] = None) -> tuple[float, float]:
        now_dt = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
        start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), start.timestamp() + 86400

    def daily_pnl(self, now: Optional[float] = None) -> float:
        lo, hi = self._day_bounds_utc(now)
        with self._lock:
            row = self.conn.execute(
                """SELECT COALESCE(SUM(pnl), 0) AS p FROM trades
                   WHERE status='closed' AND closed_at>=? AND closed_at<?""",
                (lo, hi)).fetchone()
        return float(row["p"])

    def trades_opened_today(self, now: Optional[float] = None) -> int:
        lo, hi = self._day_bounds_utc(now)
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE opened_at>=? AND opened_at<?",
                (lo, hi)).fetchone()
        return int(row["n"])

    # ---------------------------------------------------------------- kv

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
