"""
Backtester: replays the EXACT live pipeline (regime -> council -> risk ->
learning) over historic or synthetic candles.

Honest limitations:
  * Candle-based simulation -- the intrabar path is unknown.  When both the
    stop and the target sit inside one candle, the STOP is assumed to hit
    first (conservative).
  * Synthetic data proves the mechanics work, NOT that the bot is
    profitable.  Only real candles (or better, live paper trading) can
    hint at that -- and past results still never guarantee future ones.

Useful trick: run a backtest with --learn-db bot_data.sqlite3 to warm-start
the live bot's strategy weights and combo statistics from history.
"""

from __future__ import annotations

import csv
import logging
import random
from dataclasses import dataclass, field

from .config import Config
from .council import TradeCouncil
from .indicators import Candle
from .journal import Journal, TradeRecord
from .learning import PerformanceCoach
from .news import NewsSentry
from .risk import RiskManager
from .oanda import FALLBACK_SPECS

log = logging.getLogger("backtest")

TYPICAL_SPREAD = {"EUR_USD": 0.00013, "XAU_USD": 0.35}
SYNTH_START_PRICE = {"EUR_USD": 1.08, "XAU_USD": 3300.0}
SYNTH_CANDLE_VOL = {"EUR_USD": 0.00035, "XAU_USD": 1.4}


# --------------------------------------------------------------------------
# data sources
# --------------------------------------------------------------------------

def load_csv_candles(path: str) -> list[Candle]:
    """CSV columns: time,open,high,low,close[,volume]; time = unix seconds
    or 'YYYY-MM-DD HH:MM:SS' UTC."""
    from datetime import datetime, timezone
    out: list[Candle] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            t = row["time"].strip()
            if t.replace(".", "").isdigit():
                ts = float(t)
            else:
                ts = (datetime.fromisoformat(t)
                      .replace(tzinfo=timezone.utc).timestamp())
            out.append(Candle(
                time=ts, open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume") or 0)))
    out.sort(key=lambda c: c.time)
    return out


def _next_weekday_ts(ts: float, step: int = 300) -> float:
    """Advance one M5 step, skipping Saturday/Sunday."""
    from datetime import datetime, timezone, timedelta
    ts += step
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    while dt.weekday() >= 5:
        dt += timedelta(hours=12)
        dt = dt.replace(minute=0, second=0)
        ts = dt.timestamp()
    return ts


def synthetic_candles(instrument: str, n: int = 8000,
                      seed: int = 42) -> list[Candle]:
    """Regime-switching random walk: trends, ranges and volatility bursts."""
    rng = random.Random(seed)
    price = SYNTH_START_PRICE.get(instrument, 100.0)
    vol = SYNTH_CANDLE_VOL.get(instrument, price * 0.0004)
    # start on a Monday 00:00 UTC (2026-01-05)
    ts = 1767571200.0
    out: list[Candle] = []
    remaining, drift, vol_mult, anchor = 0, 0.0, 1.0, price
    while len(out) < n:
        if remaining <= 0:
            regime = rng.choice(["up", "down", "range", "volatile"])
            remaining = rng.randint(300, 900)
            anchor = price
            drift = {"up": 0.18, "down": -0.18, "range": 0.0,
                     "volatile": 0.0}[regime] * vol
            vol_mult = 2.4 if regime == "volatile" else 1.0
        step_vol = vol * vol_mult
        pull = (anchor - price) * 0.004 if drift == 0 else 0.0
        o = price
        c = o + drift + pull + rng.gauss(0, step_vol)
        hi = max(o, c) + abs(rng.gauss(0, step_vol * 0.5))
        lo = min(o, c) - abs(rng.gauss(0, step_vol * 0.5))
        out.append(Candle(time=ts, open=o, high=hi, low=max(lo, 1e-9),
                          close=max(c, 1e-9), volume=rng.randint(50, 500)))
        price = max(c, 1e-9)
        ts = _next_weekday_ts(ts)
        remaining -= 1
    return out


def aggregate_h1(m5: list[Candle]) -> list[Candle]:
    out: list[Candle] = []
    for c in m5:
        bucket = int(c.time // 3600) * 3600
        if out and out[-1].time == bucket:
            last = out[-1]
            last.high = max(last.high, c.high)
            last.low = min(last.low, c.low)
            last.close = c.close
            last.volume += c.volume
        else:
            out.append(Candle(time=bucket, open=c.open, high=c.high,
                              low=c.low, close=c.close, volume=c.volume))
    return out


# --------------------------------------------------------------------------
# simulation
# --------------------------------------------------------------------------

@dataclass
class BacktestResult:
    instrument: str
    start_balance: float
    end_balance: float
    trades: int = 0
    wins: int = 0
    total_r: float = 0.0
    max_drawdown_pct: float = 0.0
    vetoed_or_passed: int = 0
    coach_report: str = ""
    equity_curve: list[float] = field(default_factory=list)

    def summary(self) -> str:
        wr = self.wins / self.trades * 100 if self.trades else 0.0
        exp = self.total_r / self.trades if self.trades else 0.0
        ret = (self.end_balance / self.start_balance - 1) * 100
        return (
            f"===== backtest: {self.instrument} =====\n"
            f"balance      : {self.start_balance:,.2f} -> {self.end_balance:,.2f} "
            f"({ret:+.2f}%)\n"
            f"trades       : {self.trades} (win rate {wr:.1f}%)\n"
            f"expectancy   : {exp:+.3f} R per trade, total {self.total_r:+.1f} R\n"
            f"max drawdown : {self.max_drawdown_pct:.2f}%\n"
            f"decisions where council declined: {self.vetoed_or_passed}\n"
            f"{self.coach_report}")


class Backtester:
    def __init__(self, cfg: Config, instrument: str,
                 candles: list[Candle], start_balance: float = 10_000.0,
                 journal: Journal | None = None):
        self.cfg = cfg
        self.instrument = instrument
        self.candles = candles
        self.balance = start_balance
        self.start_balance = start_balance
        self.journal = journal or Journal(":memory:")
        self.coach = PerformanceCoach(cfg.learning, self.journal)
        news = NewsSentry(cfg.news)          # news checks are skipped in replay
        self.council = TradeCouncil(cfg.council, self.coach, news)
        self.risk = RiskManager(cfg.risk, self.journal)
        self.spread = TYPICAL_SPREAD.get(instrument, 0.0)
        self.spec = FALLBACK_SPECS.get(instrument) or next(
            iter(FALLBACK_SPECS.values()))

    def run(self, warmup: int = 300) -> BacktestResult:
        res = BacktestResult(self.instrument, self.start_balance,
                             self.start_balance)
        h1_all = aggregate_h1(self.candles)
        h1_idx = 0
        open_rec: TradeRecord | None = None
        peak = self.balance

        for i in range(warmup, len(self.candles)):
            candle = self.candles[i]
            now = candle.time
            while (h1_idx < len(h1_all)
                   and h1_all[h1_idx].time + 3600 <= now):
                h1_idx += 1
            h1 = h1_all[max(0, h1_idx - 200):h1_idx + 1]

            # ---- manage the open trade against this candle (stop first)
            if open_rec is not None:
                d = open_rec.direction
                hit_sl = (candle.low <= open_rec.stop_price if d > 0
                          else candle.high >= open_rec.stop_price)
                hit_tp = (candle.high >= open_rec.tp_price if d > 0
                          else candle.low <= open_rec.tp_price)
                exit_price = None
                if hit_sl:                    # conservative: stop first
                    exit_price = open_rec.stop_price
                elif hit_tp:
                    exit_price = open_rec.tp_price
                if exit_price is not None:
                    pnl = (exit_price - open_rec.entry_price) * d * open_rec.units
                    self.balance += pnl
                    closed = self.journal.record_close(
                        open_rec.id, exit_price, pnl, closed_at=now)
                    if closed:
                        self.coach.learn_from_close(
                            closed, self.cfg.council.base_score_threshold)
                        res.trades += 1
                        res.wins += 1 if pnl > 0 else 0
                        res.total_r += closed.r_multiple or 0.0
                    open_rec = None
                else:
                    new_stop = self.risk.breakeven_stop(
                        d, open_rec.entry_price, open_rec.stop_price,
                        candle.close)
                    if new_stop is not None:
                        open_rec.stop_price = new_stop

            peak = max(peak, self.balance)
            dd = (peak - self.balance) / peak * 100 if peak > 0 else 0.0
            res.max_drawdown_pct = max(res.max_drawdown_pct, dd)
            res.equity_curve.append(self.balance)

            if open_rec is not None:
                continue
            gate = self.risk.daily_gate(self.balance, now, 0)
            if gate:
                continue

            window = self.candles[max(0, i - self.cfg.engine.candle_count):i + 1]
            decision = self.council.evaluate(
                self.instrument, window, h1, self.spread, now,
                check_news=False)
            if not decision.approved:
                if decision.signals:
                    res.vetoed_or_passed += 1
                continue

            price = candle.close + (self.spread / 2) * decision.direction
            sized = self.risk.size(decision.direction, price,
                                   decision.atr or 0.0, self.balance, self.spec)
            if sized is None:
                continue
            open_rec = TradeRecord(
                instrument=self.instrument, direction=decision.direction,
                units=abs(sized.units), entry_price=price,
                stop_price=sized.stop_price, tp_price=sized.tp_price,
                risk_usd=sized.risk_usd, atr=decision.atr,
                regime=decision.regime,
                confirmations=decision.confirmations,
                council_score=decision.score, opened_at=now, paper=True,
                broker_trade_id=f"bt-{i}",
            )
            self.journal.record_open(open_rec)

        # close any trade left open at the end, at the last close price
        if open_rec is not None:
            last = self.candles[-1]
            pnl = ((last.close - open_rec.entry_price)
                   * open_rec.direction * open_rec.units)
            self.balance += pnl
            self.journal.record_close(open_rec.id, last.close, pnl,
                                      closed_at=last.time)
            res.trades += 1
            res.wins += 1 if pnl > 0 else 0

        res.end_balance = self.balance
        res.coach_report = self.coach.report()
        return res
