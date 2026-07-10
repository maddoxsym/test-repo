#!/usr/bin/env python3
"""
================================================================================
XAUUSD ADAPTIVE TRADING BOT — single-file implementation
================================================================================

A fully automated, adaptive XAUUSD (gold) trading system for MetaTrader 5.

MODES
    BACKTEST : research on historical candles (CSV) or synthetic data.
    PAPER    : live analysis, simulated fills, no orders sent.   <-- DEFAULT
    DEMO     : real orders on an MT5 *demo* account.
    LIVE     : real orders on a live account. Disabled by default and gated
               behind multiple explicit safeguards (see LiveGate).

INSTALLATION
    Core engine (backtest / tests / paper simulation) is pure standard
    library and runs on any OS with Python 3.10+:

        pip install requests

    For PAPER/DEMO/LIVE against a real MT5 terminal (WINDOWS ONLY —
    the MetaTrader5 python package does not exist for Linux/macOS):

        pip install MetaTrader5 numpy pandas requests

ENVIRONMENT VARIABLES (never hard-code credentials)
    MT5_LOGIN            MT5 account number (integer)
    MT5_PASSWORD         MT5 account password
    MT5_SERVER           MT5 broker server name, e.g. "Broker-Demo"
    TELEGRAM_BOT_TOKEN   optional, for alerts
    TELEGRAM_CHAT_ID     optional, for alerts
    NEWS_API_KEY         optional, for a live economic-calendar provider

    Windows (PowerShell):   $env:MT5_LOGIN="12345678"
    Windows (cmd):          set MT5_LOGIN=12345678
    Linux/macOS:            export MT5_LOGIN=12345678

MT5 SETUP (Windows)
    1. Install the MetaTrader 5 terminal from your broker.
    2. Log in to your DEMO account first.
    3. Tools > Options > Expert Advisors > enable "Allow algorithmic trading".
    4. Leave the terminal running; the python package attaches to it.
    5. Set the environment variables above.

USAGE
    Run built-in test suite (do this first):
        python xauusd_adaptive_bot.py --test

    Backtest on synthetic data (mechanics verification, NOT profitability):
        python xauusd_adaptive_bot.py --mode BACKTEST --synthetic

    Backtest on real candle CSV (columns: time,open,high,low,close,volume
    [,spread], time = UTC "YYYY-MM-DD HH:MM:SS" or unix seconds; M5 or M1
    data recommended):
        python xauusd_adaptive_bot.py --mode BACKTEST --data candles_m5.csv

    Walk-forward analysis:
        python xauusd_adaptive_bot.py --mode BACKTEST --data candles_m5.csv --walk-forward

    Paper trading (default mode; needs MT5 terminal for live data feed):
        python xauusd_adaptive_bot.py --mode PAPER

    Demo trading (real orders on demo account):
        python xauusd_adaptive_bot.py --mode DEMO

    LIVE trading (all of the following are required, none default on):
        1. Set MODE = "LIVE" via --mode LIVE
        2. Set LIVE_TRADING_ENABLED = True in the CONFIG section below
        3. Pass the explicit flag --i-understand-live-risk
        4. Set live_account_number / live_server in config to match the
           connected account exactly.
        5. Config validation, and the paper/demo/backtest verification
           flags in config, must all pass.
        python xauusd_adaptive_bot.py --mode LIVE --i-understand-live-risk

    Emergency close everything and stop:
        python xauusd_adaptive_bot.py --emergency-close

    Kill switch: create a file named KILL_SWITCH (config.kill_switch_file)
    next to the bot; it stops opening anything new and safely halts.

DESIGN NOTES / HONEST LIMITATIONS
    * Candle-based backtesting: without tick data the intrabar path is
      unknown. When both SL and TP lie inside one candle the engine
      assumes STOP FIRST (conservative). Gaps fill at the candle open.
    * The MetaTrader5 package is Windows-only. On other platforms the bot
      refuses PAPER/DEMO/LIVE and clearly says why; BACKTEST/tests work
      everywhere.
    * Without NEWS_API_KEY the news filter uses manually configured
      blackout windows plus spread/volatility locks and LOGS that live
      news protection is incomplete. It never fabricates events.
    * Nothing here guarantees profitability. The daily +2%/+3% figures
      are throttles (risk-off levels), not promises.

This file is intentionally one module, but internally layered:
    foundation -> analysis -> decision -> execution -> research -> control
================================================================================
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import math
import os
import random
import signal as os_signal
import sqlite3
import statistics
import sys
import threading
import time as time_mod
import traceback
import unittest
import uuid
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, timedelta, timezone, date, time as dtime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Optional third-party imports. The core engine never requires them.
# ---------------------------------------------------------------------------
try:  # Windows-only; required for PAPER (live feed) / DEMO / LIVE.
    import MetaTrader5 as mt5  # type: ignore
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore
    MT5_AVAILABLE = False

try:
    import requests as _requests  # for Telegram / news providers
    REQUESTS_AVAILABLE = True
except ImportError:
    _requests = None  # type: ignore
    REQUESTS_AVAILABLE = False

UTC = timezone.utc
BOT_VERSION = "1.0.0"
BOT_NAME = "xauusd_adaptive_bot"


# ===========================================================================
# SECTION 1 — ENUMS
# ===========================================================================

class Mode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE = "LIVE"


class Timeframe(str, Enum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"
    W1 = "W1"

    @property
    def minutes(self) -> int:
        return {"M1": 1, "M5": 5, "M15": 15, "M30": 30,
                "H1": 60, "H4": 240, "D1": 1440, "W1": 10080}[self.value]

    @property
    def seconds(self) -> int:
        return self.minutes * 60


TF_ORDER: List[Timeframe] = [Timeframe.M1, Timeframe.M5, Timeframe.M15,
                             Timeframe.M30, Timeframe.H1, Timeframe.H4,
                             Timeframe.D1, Timeframe.W1]


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> "Direction":
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class Regime(str, Enum):
    STRONG_BULL = "STRONG_BULL"
    WEAK_BULL = "WEAK_BULL"
    STRONG_BEAR = "STRONG_BEAR"
    WEAK_BEAR = "WEAK_BEAR"
    RANGE = "RANGE"
    COMPRESSION = "COMPRESSION"
    EXPANSION = "EXPANSION"
    REVERSAL_ATTEMPT = "REVERSAL_ATTEMPT"
    NEWS_VOLATILITY = "NEWS_VOLATILITY"
    ABNORMAL_SPREAD = "ABNORMAL_SPREAD"
    UNSAFE = "UNSAFE"


class SwingKind(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class StructureEventKind(str, Enum):
    BOS = "BOS"            # break of structure (with-trend)
    CHOCH = "CHOCH"        # change of character (counter-trend first break)
    MSS = "MSS"            # CHoCH + displacement/sweep => market structure shift


class TrendState(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGING = "RANGING"
    UNDEFINED = "UNDEFINED"


class ZoneKind(str, Enum):
    SUPPLY = "SUPPLY"
    DEMAND = "DEMAND"


class ZonePattern(str, Enum):
    RBD = "RBD"  # rally-base-drop
    DBR = "DBR"  # drop-base-rally
    DBD = "DBD"  # drop-base-drop
    RBR = "RBR"  # rally-base-rally


class LiquidityKind(str, Enum):
    EQUAL_HIGHS = "EQUAL_HIGHS"
    EQUAL_LOWS = "EQUAL_LOWS"
    SWING_HIGH = "SWING_HIGH"
    SWING_LOW = "SWING_LOW"
    PDH = "PDH"
    PDL = "PDL"
    PWH = "PWH"
    PWL = "PWL"
    SESSION_HIGH = "SESSION_HIGH"
    SESSION_LOW = "SESSION_LOW"
    RANGE_HIGH = "RANGE_HIGH"
    RANGE_LOW = "RANGE_LOW"


class LiquidityState(str, Enum):
    UNTOUCHED = "UNTOUCHED"
    SWEPT = "SWEPT"
    RECLAIMED = "RECLAIMED"
    INVALIDATED = "INVALIDATED"
    TARGET_HIT = "TARGET_HIT"


class FVGState(str, Enum):
    UNFILLED = "UNFILLED"
    PARTIAL = "PARTIAL"
    MITIGATED = "MITIGATED"


class SetupModel(str, Enum):
    TREND_CONTINUATION = "TREND_CONTINUATION"
    LIQUIDITY_SWEEP_REVERSAL = "LIQUIDITY_SWEEP_REVERSAL"
    BREAK_RETEST = "BREAK_RETEST"
    RANGE_EXTREME = "RANGE_EXTREME"
    SESSION_LIQUIDITY = "SESSION_LIQUIDITY"
    HTF_ZONE_REACTION = "HTF_ZONE_REACTION"


class SetupGrade(str, Enum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    NO_TRADE = "NO_TRADE"

    @staticmethod
    def from_score(score: float) -> "SetupGrade":
        if score >= 90:
            return SetupGrade.A_PLUS
        if score >= 80:
            return SetupGrade.A
        if score >= 70:
            return SetupGrade.B
        return SetupGrade.NO_TRADE


class EntryMode(str, Enum):
    MARKET_ON_CONFIRM = "A"   # market entry after confirmed candle close
    LIMIT_ON_RETEST = "B"     # limit order at the retest level
    M1_PRECISION = "C"        # M1 refinement after M5/M15 confirmation
    ALERT_ONLY = "D"          # manual approval required


class TradeStatus(str, Enum):
    PENDING = "PENDING"       # limit order waiting for fill
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    PARTIAL_TP = "PARTIAL_TP"
    TRAIL_STOP = "TRAIL_STOP"
    BREAK_EVEN = "BREAK_EVEN"
    TIME_EXIT = "TIME_EXIT"
    NEWS_EXIT = "NEWS_EXIT"
    WEEKEND_EXIT = "WEEKEND_EXIT"
    EMERGENCY = "EMERGENCY"
    MANUAL = "MANUAL"
    END_OF_DATA = "END_OF_DATA"


class SessionName(str, Enum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    OVERLAP = "OVERLAP"      # London/NY overlap
    OFF_HOURS = "OFF_HOURS"


class ManagementProfile(str, Enum):
    CONSERVATIVE = "CONSERVATIVE"
    BALANCED = "BALANCED"
    AGGRESSIVE = "AGGRESSIVE"


class Verdict(str, Enum):
    LIVE_NOW = "LIVE NOW"
    WAITING_FOR_RETEST = "WAITING FOR RETEST"
    WAITING_FOR_CONFIRMATION = "WAITING FOR CONFIRMATION"
    MANAGE_ONLY = "MANAGE ONLY"
    NO_TRADE = "NO TRADE"
    SETUP_INVALIDATED = "SETUP INVALIDATED"
    NEWS_BLACKOUT = "NEWS BLACKOUT"
    SPREAD_TOO_HIGH = "SPREAD TOO HIGH"
    DAILY_RISK_LIMIT = "DAILY RISK LIMIT REACHED"
    DAILY_TARGET = "DAILY TARGET REACHED"


class LockReason(str, Enum):
    NONE = "NONE"
    DAILY_LOSS = "DAILY_LOSS"
    WEEKLY_LOSS = "WEEKLY_LOSS"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    DAILY_TARGET = "DAILY_TARGET"
    NEWS = "NEWS"
    SPREAD = "SPREAD"
    STALE_DATA = "STALE_DATA"
    DISCONNECTED = "DISCONNECTED"
    KILL_SWITCH = "KILL_SWITCH"
    MAX_TRADES_DAY = "MAX_TRADES_DAY"
    MAX_TRADES_SESSION = "MAX_TRADES_SESSION"
    SESSION_BLOCKED = "SESSION_BLOCKED"


# ===========================================================================
# SECTION 2 — CONFIGURATION
# ===========================================================================
# Edit this section (or subclass/override via CLI flags). Credentials are
# NEVER placed here — they come from environment variables only.
# ===========================================================================

@dataclass
class Config:
    # ---- mode & master safety switches -----------------------------------
    mode: Mode = Mode.PAPER                    # default mode = PAPER
    live_trading_enabled: bool = False         # hard gate; must be True for LIVE
    aggressive_mode: bool = False              # allows risk up to hard cap; OFF
    alert_only: bool = False                   # MODE D: never execute, only alert
    kill_switch_file: str = "KILL_SWITCH"

    # ---- symbol -----------------------------------------------------------
    symbol_preference: Tuple[str, ...] = (
        "XAUUSD", "XAUUSD.", "XAUUSD-STD", "XAUUSDm", "XAUUSD.a", "GOLD",
        "GOLD-STD", "GOLDm", "XAUUSD.pro", "XAUUSD.raw")
    symbol_override: str = ""                  # set to force an exact symbol
    magic_number: int = 762031

    # ---- risk policy (fractions of equity, e.g. 0.01 == 1%) ---------------
    hard_max_risk_per_trade: float = 0.05      # ABSOLUTE ceiling, never higher
    default_risk_b: float = 0.005              # B grade (70-79)   -> 0.5%
    default_risk_b_max: float = 0.01
    default_risk_a: float = 0.01               # A grade (80-89)   -> 1.0-1.5%
    default_risk_a_max: float = 0.015
    default_risk_a_plus: float = 0.02          # A+ grade (90-100) -> max 2%
    aggressive_risk_a_plus: float = 0.05       # only if aggressive_mode=True
    max_combined_open_risk: float = 0.05       # total open-position risk
    max_daily_loss: float = 0.05               # realised + unrealised
    max_weekly_drawdown: float = 0.10
    max_consecutive_losses: int = 3
    max_positions: int = 1                     # one directional XAUUSD setup
    max_trades_per_day: int = 4
    max_trades_per_session: int = 2

    # ---- daily profit throttle (targets, never forced) --------------------
    daily_profit_soft_stop: float = 0.02       # reduce risk / stop new trades
    daily_profit_hard_stop: float = 0.03       # stop opening for the day
    soft_stop_risk_factor: float = 0.5         # risk multiplier after soft stop
    broker_day_reset_hour_utc: int = 0         # equity snapshot boundary (UTC)

    # ---- costs & execution quality ----------------------------------------
    max_spread_points: float = 60.0            # reject entries above this
    normal_spread_points: float = 35.0         # used for regime/abnormal check
    max_slippage_points: float = 40.0
    commission_per_lot: float = 7.0            # round-turn USD estimate
    slippage_buffer_points: float = 15.0       # assumed in sizing & backtest
    execution_delay_candles: int = 0           # extra delay in backtest fills
    signal_max_age_seconds: int = 180          # stale-signal rejection
    max_price_drift_atr: float = 0.35          # price moved too far since signal

    # ---- reward:risk ------------------------------------------------------
    min_rr: float = 2.0                        # net-of-costs minimum
    min_rr_a_plus: float = 1.5                 # only for A+ setups
    preferred_rr_cap: float = 4.0

    # ---- structure / detection parameters ---------------------------------
    swing_left: int = 2
    swing_right: int = 2
    external_swing_left: int = 3
    external_swing_right: int = 3
    atr_period: int = 14
    displacement_atr_mult: float = 1.2
    displacement_body_ratio: float = 0.55
    bos_use_close: bool = True                 # close-based breaks (no wick BOS)
    eq_level_atr_tol: float = 0.12             # equal highs/lows tolerance
    sweep_close_back_required: bool = True
    zone_base_max_candles: int = 5
    zone_base_body_atr: float = 0.60
    zone_max_touches: int = 2                  # more touches -> stale zone
    zone_max_age_candles: int = 400
    fvg_min_size_atr: float = 0.15
    ob_require_structure_link: bool = True
    premium_discount_buffer: float = 0.05      # 45-55% counts as equilibrium
    extension_max_atr: float = 3.0             # "don't chase" distance from base
    retest_max_candles: int = 12
    range_edge_fraction: float = 0.25          # outer 25% counts as range edge
    min_stop_atr: float = 0.35                 # structural stop inside noise
    max_stop_atr: float = 3.5                  # stop too wide
    stop_buffer_atr: float = 0.25              # buffer beyond structure
    min_score: float = 70.0
    countertrend_min_score: float = 80.0       # reversals need more proof

    # ---- adaptive timeframe engine ----------------------------------------
    analysis_timeframes: Tuple[str, ...] = ("D1", "H4", "H1", "M30", "M15", "M5", "M1")
    min_candles_required: int = 120            # per TF before it is trusted
    m1_noise_efficiency_max: float = 0.18      # below -> M1 too noisy
    high_vol_atr_percentile: float = 0.85
    low_vol_atr_percentile: float = 0.25

    # ---- sessions (UTC hours) ---------------------------------------------
    user_timezone: str = "Europe/Madrid"
    asia_start_utc: int = 0
    asia_end_utc: int = 7
    london_start_utc: int = 7
    london_end_utc: int = 16
    ny_start_utc: int = 12
    ny_end_utc: int = 21
    allow_asia_entries: bool = True            # only when quality is sufficient
    asia_min_score: float = 85.0
    avoid_rollover_utc: Tuple[int, int] = (21, 23)   # no entries in this window
    friday_last_entry_hour_utc: int = 15
    friday_flat_hour_utc: int = 20             # reduce/close before weekend
    monday_first_entry_hour_utc: int = 2
    weekend_hold_allowed: bool = False

    # ---- news filter -------------------------------------------------------
    news_block_before_min: int = 30
    news_block_after_min: int = 30
    manual_blackouts_utc: Tuple[str, ...] = ()  # "YYYY-MM-DDTHH:MM/YYYY-MM-DDTHH:MM"
    news_fail_safe_block_minutes: int = 0       # >0: block when provider dead

    # ---- trade management --------------------------------------------------
    management_profile: ManagementProfile = ManagementProfile.BALANCED
    breakeven_r: float = 1.0
    breakeven_needs_structure: bool = True
    breakeven_costs_buffer: bool = True        # BE = entry +- costs
    partial_r: float = 1.7
    partial_fraction: float = 0.40             # close 40% at partial_r
    trail_timeframe: str = "M15"               # trail behind confirmed swings
    trail_atr_mult: float = 1.6                # ATR safety net for the trail
    time_exit_hours: float = 30.0              # stale-trade time stop
    max_bars_no_progress: int = 60             # candles below +0.2R -> exit

    # ---- entry mode ---------------------------------------------------------
    entry_mode: EntryMode = EntryMode.LIMIT_ON_RETEST
    allow_market_entries: bool = True          # MODE A fallback when no retest
    limit_expiry_candles: int = 10

    # ---- storage / logging --------------------------------------------------
    db_path: str = "xauusd_bot.sqlite3"
    log_dir: str = "logs"
    log_level: str = "INFO"
    journal_screenshots: bool = False          # requires a charting backend

    # ---- backtest ------------------------------------------------------------
    backtest_initial_equity: float = 10_000.0
    backtest_spread_points: float = 30.0       # used when CSV lacks spread col
    backtest_base_timeframe: str = "M5"
    backtest_warmup_candles: int = 600
    monte_carlo_runs: int = 1000
    wf_train_fraction: float = 0.5
    wf_validate_fraction: float = 0.25
    wf_folds: int = 3

    # ---- live gate (all must be satisfied for LIVE) --------------------------
    live_account_number: int = 0               # must match connected account
    live_server: str = ""                      # must match connected server
    live_min_profit_factor: float = 1.3
    live_max_drawdown: float = 0.15
    live_min_trades: int = 100
    backtest_verified: bool = False            # set True after YOUR review
    paper_verified: bool = False               # set True after paper phase
    demo_verified: bool = False                # set True after demo phase

    # ---- heartbeat / health ---------------------------------------------------
    heartbeat_seconds: int = 30
    stale_data_seconds: int = 180
    max_order_retries: int = 2
    loop_interval_seconds: float = 2.0

    # ---- credentials (loaded from environment, never hard-coded) --------------
    mt5_login: int = field(default=0, repr=False)
    mt5_password: str = field(default="", repr=False)
    mt5_server: str = field(default="", repr=False)
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)
    news_api_key: str = field(default="", repr=False)

    def load_env(self) -> "Config":
        """Populate credentials from environment variables."""
        self.mt5_login = int(os.environ.get("MT5_LOGIN", "0") or 0)
        self.mt5_password = os.environ.get("MT5_PASSWORD", "")
        self.mt5_server = os.environ.get("MT5_SERVER", "")
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.news_api_key = os.environ.get("NEWS_API_KEY", "")
        return self

    def public_dict(self) -> Dict[str, Any]:
        """Config as dict with secrets removed — safe to journal."""
        d = asdict(self)
        for k in ("mt5_login", "mt5_password", "mt5_server",
                  "telegram_token", "telegram_chat_id", "news_api_key"):
            d.pop(k, None)
        d["mode"] = self.mode.value
        d["management_profile"] = self.management_profile.value
        d["entry_mode"] = self.entry_mode.value
        return d

    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.public_dict(), sort_keys=True, default=str)
            .encode()).hexdigest()[:16]


class ConfigValidator:
    """Validates a Config before the bot is allowed to do anything."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate(self) -> bool:
        c = self.cfg
        err = self.errors.append
        warn = self.warnings.append

        if not 0 < c.hard_max_risk_per_trade <= 0.05:
            err("hard_max_risk_per_trade must be in (0, 0.05] — 5% is the "
                "absolute ceiling and cannot be raised")
        for name in ("default_risk_b", "default_risk_b_max", "default_risk_a",
                     "default_risk_a_max", "default_risk_a_plus"):
            v = getattr(c, name)
            if not 0 < v <= c.hard_max_risk_per_trade:
                err(f"{name}={v} must be in (0, hard cap {c.hard_max_risk_per_trade}]")
        if c.aggressive_risk_a_plus > c.hard_max_risk_per_trade:
            err("aggressive_risk_a_plus may never exceed the 5% hard cap")
        if c.default_risk_a_plus > 0.02:
            err("default_risk_a_plus must be <= 2% (aggressive mode is the "
                "only path above that, and it is explicit)")
        if not 0 < c.max_combined_open_risk <= 0.05:
            err("max_combined_open_risk must be in (0, 0.05]")
        if not 0 < c.max_daily_loss <= 0.05:
            err("max_daily_loss must be in (0, 0.05]")
        if not 0 < c.max_weekly_drawdown <= 0.20:
            err("max_weekly_drawdown must be in (0, 0.20]")
        if c.max_consecutive_losses < 1:
            err("max_consecutive_losses must be >= 1")
        if c.max_positions < 1:
            err("max_positions must be >= 1")
        if c.max_trades_per_day < 1 or c.max_trades_per_session < 1:
            err("max trades per day/session must be >= 1")
        if c.daily_profit_soft_stop >= c.daily_profit_hard_stop:
            err("daily_profit_soft_stop must be below daily_profit_hard_stop")
        if c.min_rr < 1.0:
            err("min_rr below 1.0 is not acceptable")
        if c.min_rr_a_plus < 1.5:
            err("min_rr_a_plus must be >= 1.5")
        if c.min_score < 70:
            err("min_score below 70 violates the no-trade threshold")
        if c.swing_left < 1 or c.swing_right < 1:
            err("swing detection needs at least 1 candle each side")
        if c.atr_period < 5:
            err("atr_period too small to be meaningful")
        if not 0 < c.wf_train_fraction < 1 or not 0 < c.wf_validate_fraction < 1:
            err("walk-forward fractions must be in (0,1)")
        if c.wf_train_fraction + c.wf_validate_fraction >= 1.0:
            err("wf_train_fraction + wf_validate_fraction must be < 1")
        if c.mode == Mode.LIVE:
            if not c.live_trading_enabled:
                err("MODE=LIVE but LIVE_TRADING_ENABLED is False")
            if c.live_account_number <= 0 or not c.live_server:
                err("LIVE mode requires live_account_number and live_server "
                    "to be configured for account matching")
            if not (c.backtest_verified and c.paper_verified and c.demo_verified):
                err("LIVE mode requires backtest_verified, paper_verified and "
                    "demo_verified to all be True (set them only after real "
                    "verification)")
        if c.aggressive_mode:
            warn("AGGRESSIVE_MODE is enabled — A+ setups may risk up to "
                 f"{c.aggressive_risk_a_plus:.1%}. This should be a deliberate choice.")
        if c.weekend_hold_allowed:
            warn("weekend_hold_allowed=True — positions can gap over the weekend")
        if not c.news_api_key:
            warn("NEWS_API_KEY not set — live news protection is incomplete; "
                 "manual blackout windows + spread locks are the only guard")
        return not self.errors

    def report(self) -> str:
        lines = []
        for e in self.errors:
            lines.append(f"CONFIG ERROR: {e}")
        for w in self.warnings:
            lines.append(f"CONFIG WARNING: {w}")
        return "\n".join(lines) if lines else "config OK"


# ===========================================================================
# SECTION 3 — LOGGING
# ===========================================================================

class LoggerManager:
    """Structured logging to console + rotating file."""

    _configured = False

    @classmethod
    def setup(cls, cfg: Config) -> logging.Logger:
        logger = logging.getLogger(BOT_NAME)
        if cls._configured:
            return logger
        logger.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S")
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        try:
            Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                Path(cfg.log_dir) / f"{BOT_NAME}.log",
                maxBytes=10_000_000, backupCount=10, encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError as exc:  # keep running on read-only filesystems
            logger.warning("file logging unavailable: %s", exc)
        cls._configured = True
        return logger

    @staticmethod
    def get() -> logging.Logger:
        return logging.getLogger(BOT_NAME)


log = LoggerManager.get()


# ===========================================================================
# SECTION 4 — DATA MODEL
# ===========================================================================

@dataclass(frozen=True)
class Candle:
    """One completed OHLC candle. time = open time, UTC, timezone-aware."""
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread: float = 0.0          # in points at candle time (0 = unknown)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_ratio(self) -> float:
        return self.body / self.range if self.range > 0 else 0.0

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


@dataclass(frozen=True)
class SwingPoint:
    index: int                   # index into the candle series it came from
    time: datetime
    price: float
    kind: SwingKind
    confirmed_index: int         # candle index at which this swing confirmed


@dataclass
class StructureEvent:
    kind: StructureEventKind
    direction: Direction         # direction the break points to
    index: int                   # candle index of the breaking close
    time: datetime
    broken_level: float          # the structural price that was broken
    swing: Optional[SwingPoint]  # the swing whose level broke
    displacement: bool = False
    swept_liquidity: bool = False


@dataclass
class DealingRange:
    low: float
    high: float
    low_time: datetime
    high_time: datetime

    def position_of(self, price: float) -> float:
        """0.0 = range low, 1.0 = range high."""
        if self.high <= self.low:
            return 0.5
        return (price - self.low) / (self.high - self.low)


@dataclass
class Zone:
    zone_id: str
    kind: ZoneKind
    pattern: ZonePattern
    upper: float
    lower: float
    timeframe: Timeframe
    created_time: datetime
    created_index: int
    freshness: float = 1.0            # 1.0 fresh -> decays with touches/age
    displacement_score: float = 0.0   # 0..1, strength of the departure
    touches: int = 0
    invalidated: bool = False
    caused_bos: bool = False
    fvg_overlap: bool = False
    ob_overlap: bool = False
    htf_aligned: bool = False

    @property
    def mid(self) -> float:
        return (self.upper + self.lower) / 2.0

    @property
    def height(self) -> float:
        return self.upper - self.lower

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    def quality(self) -> float:
        """Composite 0..1 quality score for scoring/journaling."""
        q = 0.35 * self.freshness + 0.35 * self.displacement_score
        q += 0.15 if self.caused_bos else 0.0
        q += 0.075 if self.fvg_overlap else 0.0
        q += 0.075 if self.htf_aligned else 0.0
        if self.invalidated:
            q = 0.0
        return max(0.0, min(1.0, q))


@dataclass
class FairValueGap:
    fvg_id: str
    direction: Direction              # LONG = bullish gap (support)
    upper: float
    lower: float
    timeframe: Timeframe
    created_time: datetime
    created_index: int
    state: FVGState = FVGState.UNFILLED
    fill_fraction: float = 0.0
    from_displacement: bool = False
    htf_overlap: bool = False

    @property
    def midpoint(self) -> float:      # consequent encroachment
        return (self.upper + self.lower) / 2.0

    @property
    def size(self) -> float:
        return self.upper - self.lower


@dataclass
class OrderBlock:
    ob_id: str
    direction: Direction              # LONG = bullish OB (demand candle)
    upper: float
    lower: float
    timeframe: Timeframe
    created_time: datetime
    created_index: int
    freshness: float = 1.0
    mitigations: int = 0
    invalidated: bool = False
    linked_structure: Optional[StructureEventKind] = None
    linked_sweep: bool = False
    linked_fvg: bool = False

    @property
    def mid(self) -> float:
        return (self.upper + self.lower) / 2.0


@dataclass
class LiquidityLevel:
    level_id: str
    kind: LiquidityKind
    price: float
    time: datetime
    buy_side: bool                    # True = liquidity above price (BSL)
    state: LiquidityState = LiquidityState.UNTOUCHED
    swept_time: Optional[datetime] = None
    member_prices: Tuple[float, ...] = ()   # for equal highs/lows clusters


@dataclass
class SweepEvent:
    level: LiquidityLevel
    index: int
    time: datetime
    extreme: float                    # the sweep wick extreme
    closed_back: bool
    displaced_away: bool

    @property
    def valid(self) -> bool:
        # objective sweep: traded through AND (close back inside OR displacement)
        return self.closed_back or self.displaced_away


@dataclass
class TimeframePlan:
    bias_tf: Timeframe
    structure_tf: Timeframe
    decision_tf: Timeframe
    entry_tf: Timeframe
    management_tf: Timeframe
    reason: str

    def as_dict(self) -> Dict[str, str]:
        return {"bias": self.bias_tf.value, "structure": self.structure_tf.value,
                "decision": self.decision_tf.value, "entry": self.entry_tf.value,
                "management": self.management_tf.value, "reason": self.reason}


@dataclass
class RegimeReading:
    regime: Regime
    trend: TrendState
    confidence: float                 # 0..1
    atr: float
    atr_percentile: float
    efficiency: float
    reason: str


@dataclass
class ScoreBreakdown:
    htf_alignment: float = 0.0        # /15
    zone_quality: float = 0.0         # /15
    liquidity_sweep: float = 0.0      # /15
    structure_confirmation: float = 0.0  # /15
    displacement: float = 0.0         # /10
    confluence: float = 0.0           # /10 (FVG / OB)
    premium_discount: float = 0.0     # /5
    session_quality: float = 0.0      # /5
    news_safety: float = 0.0          # /5
    target_quality: float = 0.0       # /5

    @property
    def total(self) -> float:
        return round(self.htf_alignment + self.zone_quality + self.liquidity_sweep
                     + self.structure_confirmation + self.displacement
                     + self.confluence + self.premium_discount
                     + self.session_quality + self.news_safety
                     + self.target_quality, 2)

    def as_dict(self) -> Dict[str, float]:
        d = asdict(self)
        d["total"] = self.total
        return d


@dataclass
class Setup:
    setup_id: str
    model: SetupModel
    direction: Direction
    created_time: datetime            # close time of confirming candle
    signal_price: float               # price when signal was generated
    entry_price: float                # planned entry (market ref or limit)
    stop_price: float
    tp1: float
    tp2: float
    runner_target: Optional[float]
    entry_mode: EntryMode
    score: float
    grade: SetupGrade
    breakdown: ScoreBreakdown
    tf_plan: TimeframePlan
    regime: Regime
    session: SessionName
    htf_bias: TrendState
    zone: Optional[Zone] = None
    sweep: Optional[SweepEvent] = None
    structure_event: Optional[StructureEvent] = None
    fvg: Optional[FairValueGap] = None
    order_block: Optional[OrderBlock] = None
    atr: float = 0.0
    spread_points: float = 0.0
    reason: str = ""

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)

    def rr_to(self, target: float) -> float:
        if self.stop_distance <= 0:
            return 0.0
        return abs(target - self.entry_price) / self.stop_distance


@dataclass
class PartialFill:
    time: datetime
    price: float
    volume: float
    reason: ExitReason
    profit: float


@dataclass
class Trade:
    trade_id: str
    setup: Setup
    status: TradeStatus
    volume: float                     # remaining open volume
    initial_volume: float
    risk_fraction: float              # of equity at entry decision
    risk_money: float
    entry_price: float = 0.0          # actual fill
    entry_time: Optional[datetime] = None
    stop_price: float = 0.0           # current (may trail, never widens)
    initial_stop: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: Optional[ExitReason] = None
    profit: float = 0.0               # realised, includes partials & costs
    commission: float = 0.0
    partials: List[PartialFill] = field(default_factory=list)
    breakeven_done: bool = False
    partial_done: bool = False
    mfe: float = 0.0                  # max favourable excursion in R
    mae: float = 0.0                  # max adverse excursion in R
    bars_open: int = 0
    mt5_ticket: int = 0
    pending_expiry: Optional[datetime] = None

    @property
    def direction(self) -> Direction:
        return self.setup.direction

    def r_multiple(self) -> float:
        return self.profit / self.risk_money if self.risk_money > 0 else 0.0


@dataclass
class SymbolSpecification:
    """Actual broker specification — never assume a universal gold pip."""
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float                 # account-currency value of one tick per lot
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: float         # broker min stop distance in points
    freeze_level_points: float
    spread_points: float
    trade_allowed: bool = True
    currency_profit: str = "USD"
    currency_account: str = "USD"
    filling_modes: Tuple[str, ...] = ("IOC",)

    def money_per_price_unit_per_lot(self) -> float:
        """Account-currency P/L of a 1.0-unit price move for 1.0 lot."""
        if self.tick_size <= 0:
            return 0.0
        return self.tick_value / self.tick_size

    def round_volume_down(self, volume: float) -> float:
        if volume < self.volume_min:
            return 0.0
        stepped = math.floor((volume + 1e-12) / self.volume_step) * self.volume_step
        stepped = min(stepped, self.volume_max)
        # normalise float noise to step precision
        decimals = max(0, -int(math.floor(math.log10(self.volume_step))))
        return round(stepped, decimals)


def default_xauusd_spec() -> SymbolSpecification:
    """Representative XAUUSD spec used ONLY for backtests and tests.
    Live/paper/demo always read the real spec from MT5."""
    return SymbolSpecification(
        name="XAUUSD", digits=2, point=0.01, tick_size=0.01, tick_value=1.0,
        contract_size=100.0, volume_min=0.01, volume_max=100.0,
        volume_step=0.01, stops_level_points=20.0, freeze_level_points=0.0,
        spread_points=30.0)


# ===========================================================================
# SECTION 5 — SMALL UTILITIES
# ===========================================================================

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def true_range(prev_close: float, c: Candle) -> float:
    return max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))


def atr_series(candles: Sequence[Candle], period: int) -> List[float]:
    """Wilder ATR. atr[i] uses candles up to and including i (no look-ahead)."""
    n = len(candles)
    out = [0.0] * n
    if n == 0:
        return out
    trs = [candles[0].range]
    for i in range(1, n):
        trs.append(true_range(candles[i - 1].close, candles[i]))
    if n < period:
        run = 0.0
        for i in range(n):
            run += trs[i]
            out[i] = run / (i + 1)
        return out
    first = sum(trs[:period]) / period
    for i in range(period):
        out[i] = sum(trs[:i + 1]) / (i + 1)
    out[period - 1] = first
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    return out


def atr_at(candles: Sequence[Candle], period: int) -> float:
    if not candles:
        return 0.0
    return atr_series(candles, period)[-1]


def percentile_rank(values: Sequence[float], x: float) -> float:
    """Fraction of values <= x. 0..1."""
    if not values:
        return 0.5
    return sum(1 for v in values if v <= x) / len(values)


def efficiency_ratio(candles: Sequence[Candle], lookback: int) -> float:
    """Kaufman efficiency: |net move| / sum(|candle-to-candle moves|)."""
    if len(candles) < lookback + 1:
        return 0.0
    window = candles[-(lookback + 1):]
    net = abs(window[-1].close - window[0].close)
    path = sum(abs(window[i].close - window[i - 1].close)
               for i in range(1, len(window)))
    return net / path if path > 0 else 0.0


def rate_of_change(candles: Sequence[Candle], lookback: int) -> float:
    if len(candles) < lookback + 1 or candles[-(lookback + 1)].close == 0:
        return 0.0
    return (candles[-1].close - candles[-(lookback + 1)].close) / candles[-(lookback + 1)].close


def is_displacement(candle: Candle, atr: float, cfg: Config) -> bool:
    """Objective displacement: large body relative to ATR, dominant body."""
    if atr <= 0:
        return False
    return (candle.body >= cfg.displacement_atr_mult * atr
            and candle.body_ratio >= cfg.displacement_body_ratio)


def round_number_levels(price: float, step: float = 50.0, count: int = 3) -> List[float]:
    """Psychological levels near price (e.g. 4000, 4050 style, computed fresh)."""
    base = math.floor(price / step) * step
    out = []
    for k in range(-count, count + 1):
        out.append(base + k * step)
    return out


def tf_bucket_start(t: datetime, tf: Timeframe) -> datetime:
    """UTC-aligned open time of the tf bucket containing t."""
    t = t.astimezone(UTC)
    if tf == Timeframe.W1:
        d = t.date() - timedelta(days=t.weekday())  # Monday
        return datetime(d.year, d.month, d.day, tzinfo=UTC)
    if tf == Timeframe.D1:
        return datetime(t.year, t.month, t.day, tzinfo=UTC)
    mins = tf.minutes
    total = t.hour * 60 + t.minute
    start = (total // mins) * mins
    return datetime(t.year, t.month, t.day, start // 60, start % 60, tzinfo=UTC)


def resample(candles: Sequence[Candle], tf: Timeframe,
             completed_only: bool = True,
             now: Optional[datetime] = None) -> List[Candle]:
    """Aggregate base candles into tf candles. A bucket is emitted only when
    it is complete relative to `now` (default: close time of last base
    candle). No future data can leak: buckets are built strictly from base
    candles whose open time falls inside the bucket."""
    if not candles:
        return []
    base_tf_seconds = None
    if len(candles) >= 2:
        base_tf_seconds = int((candles[1].time - candles[0].time).total_seconds())
    if now is None:
        # close time of the final base candle
        last = candles[-1]
        step = base_tf_seconds or 60
        now = last.time + timedelta(seconds=step)
    out: List[Candle] = []
    cur_start: Optional[datetime] = None
    o = h = l = c = 0.0
    vol = 0.0
    spr = 0.0
    n_in = 0
    for cd in candles:
        b = tf_bucket_start(cd.time, tf)
        if cur_start is None or b != cur_start:
            if cur_start is not None and n_in > 0:
                out.append(Candle(cur_start, o, h, l, c, vol, spr / max(n_in, 1)))
            cur_start, o, h, l, c = b, cd.open, cd.high, cd.low, cd.close
            vol, spr, n_in = cd.volume, cd.spread, 1
        else:
            h = max(h, cd.high)
            l = min(l, cd.low)
            c = cd.close
            vol += cd.volume
            spr += cd.spread
            n_in += 1
    if cur_start is not None and n_in > 0:
        bucket_end = cur_start + timedelta(minutes=tf.minutes)
        if not completed_only or now >= bucket_end:
            out.append(Candle(cur_start, o, h, l, c, vol, spr / max(n_in, 1)))
    return out


def parse_csv_candles(path: str) -> List[Candle]:
    """Load candles from CSV: time,open,high,low,close[,volume[,spread]].
    time = unix seconds or 'YYYY-MM-DD HH:MM[:SS]' (UTC assumed)."""
    candles: List[Candle] = []
    with open(path, "r", encoding="utf-8") as fh:
        header_skipped = False
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            parts = [p.strip() for p in raw.replace(";", ",").split(",")]
            if not header_skipped and not _looks_numericish(parts[1] if len(parts) > 1 else ""):
                header_skipped = True
                continue
            header_skipped = True
            if len(parts) < 5:
                continue
            t = _parse_time(parts[0])
            if t is None:
                continue
            try:
                o, h, l, c = (float(parts[1]), float(parts[2]),
                              float(parts[3]), float(parts[4]))
                v = float(parts[5]) if len(parts) > 5 and parts[5] else 0.0
                s = float(parts[6]) if len(parts) > 6 and parts[6] else 0.0
            except ValueError:
                continue
            candles.append(Candle(t, o, h, l, c, v, s))
    candles.sort(key=lambda x: x.time)
    # drop duplicates by open time
    dedup: List[Candle] = []
    seen = set()
    for cd in candles:
        if cd.time not in seen:
            seen.add(cd.time)
            dedup.append(cd)
    return dedup


def _looks_numericish(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _parse_time(s: str) -> Optional[datetime]:
    s = s.strip()
    if _looks_numericish(s):
        try:
            return datetime.fromtimestamp(float(s), tz=UTC)
        except (ValueError, OverflowError, OSError):
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


# ===========================================================================
# SECTION 6 — DATABASE (SQLite; optional PostgreSQL DSN hook)
# ===========================================================================

class DatabaseManager:
    """SQLite persistence. Everything journaled lives here. A PostgreSQL DSN
    can be configured by swapping the connection factory, but SQLite works
    with zero infrastructure and is the default."""

    SCHEMA: Tuple[str, ...] = (
        """CREATE TABLE IF NOT EXISTS trades (
            trade_id TEXT PRIMARY KEY, setup_id TEXT, mode TEXT, symbol TEXT,
            model TEXT, direction TEXT, status TEXT, grade TEXT, score REAL,
            entry_time TEXT, entry_price REAL, initial_stop REAL,
            stop_price REAL, tp1 REAL, tp2 REAL, volume REAL,
            initial_volume REAL, risk_fraction REAL, risk_money REAL,
            exit_time TEXT, exit_price REAL, exit_reason TEXT,
            profit REAL, commission REAL, r_multiple REAL, mfe REAL, mae REAL,
            bars_open INTEGER, mt5_ticket INTEGER, session TEXT, regime TEXT,
            tf_plan TEXT, spread REAL, atr REAL, version TEXT,
            details TEXT)""",
        """CREATE TABLE IF NOT EXISTS signals (
            setup_id TEXT PRIMARY KEY, time TEXT, mode TEXT, symbol TEXT,
            model TEXT, direction TEXT, score REAL, grade TEXT,
            entry_price REAL, stop_price REAL, tp1 REAL, tp2 REAL,
            entry_mode TEXT, session TEXT, regime TEXT, htf_bias TEXT,
            tf_plan TEXT, breakdown TEXT, reason TEXT, accepted INTEGER,
            version TEXT)""",
        """CREATE TABLE IF NOT EXISTS rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, setup_id TEXT,
            model TEXT, direction TEXT, score REAL, stage TEXT, reason TEXT,
            details TEXT)""",
        """CREATE TABLE IF NOT EXISTS zones (
            zone_id TEXT PRIMARY KEY, kind TEXT, pattern TEXT, timeframe TEXT,
            upper REAL, lower REAL, created TEXT, freshness REAL,
            displacement REAL, touches INTEGER, invalidated INTEGER,
            caused_bos INTEGER)""",
        """CREATE TABLE IF NOT EXISTS liquidity (
            level_id TEXT PRIMARY KEY, kind TEXT, price REAL, time TEXT,
            buy_side INTEGER, state TEXT, swept_time TEXT)""",
        """CREATE TABLE IF NOT EXISTS daily_stats (
            day TEXT PRIMARY KEY, start_equity REAL, end_equity REAL,
            realised REAL, trades INTEGER, wins INTEGER, losses INTEGER,
            max_drawdown REAL, locked TEXT)""",
        """CREATE TABLE IF NOT EXISTS weekly_stats (
            week TEXT PRIMARY KEY, start_equity REAL, end_equity REAL,
            realised REAL, trades INTEGER, max_drawdown REAL)""",
        """CREATE TABLE IF NOT EXISTS monthly_stats (
            month TEXT PRIMARY KEY, start_equity REAL, end_equity REAL,
            realised REAL, trades INTEGER)""",
        """CREATE TABLE IF NOT EXISTS config_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, version TEXT,
            config_hash TEXT, config_json TEXT, note TEXT)""",
        """CREATE TABLE IF NOT EXISTS param_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, name TEXT,
            old_value TEXT, new_value TEXT, evidence TEXT)""",
        """CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, level TEXT,
            component TEXT, message TEXT, tb TEXT)""",
        """CREATE TABLE IF NOT EXISTS heartbeats (
            id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, mode TEXT,
            equity REAL, open_trades INTEGER, lock TEXT, note TEXT)""",
        """CREATE TABLE IF NOT EXISTS backtest_runs (
            run_id TEXT PRIMARY KEY, time TEXT, data_desc TEXT,
            config_hash TEXT, start TEXT, end TEXT, trades INTEGER,
            net_return REAL, profit_factor REAL, expectancy_r REAL,
            max_dd REAL, metrics TEXT)""",
        """CREATE TABLE IF NOT EXISTS walkforward_runs (
            run_id TEXT PRIMARY KEY, time TEXT, folds INTEGER,
            summary TEXT)""",
        """CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY, value TEXT)""",
    )

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock, self.conn:
            for stmt in self.SCHEMA:
                self.conn.execute(stmt)

    # -- generic helpers ----------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock, self.conn:
            self.conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple]:
        with self._lock:
            cur = self.conn.execute(sql, params)
            return cur.fetchall()

    def set_state(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO bot_state(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, default=str)))

    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM bot_state WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0][0])
        except (json.JSONDecodeError, TypeError):
            return default

    # -- journaling ---------------------------------------------------------
    def journal_signal(self, setup: Setup, accepted: bool, mode: Mode,
                       symbol: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (setup.setup_id, setup.created_time.isoformat(), mode.value, symbol,
             setup.model.value, setup.direction.value, setup.score,
             setup.grade.value, setup.entry_price, setup.stop_price,
             setup.tp1, setup.tp2, setup.entry_mode.value,
             setup.session.value, setup.regime.value, setup.htf_bias.value,
             json.dumps(setup.tf_plan.as_dict()),
             json.dumps(setup.breakdown.as_dict()), setup.reason,
             1 if accepted else 0, BOT_VERSION))

    def journal_rejection(self, time_: datetime, setup_id: str, model: str,
                          direction: str, score: float, stage: str,
                          reason: str, details: str = "") -> None:
        self.execute(
            "INSERT INTO rejections(time,setup_id,model,direction,score,stage,reason,details) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (time_.isoformat(), setup_id, model, direction, score, stage,
             reason, details))

    def journal_trade(self, trade: Trade, mode: Mode, symbol: str) -> None:
        s = trade.setup
        details = {
            "partials": [{"time": p.time.isoformat(), "price": p.price,
                          "volume": p.volume, "reason": p.reason.value,
                          "profit": p.profit} for p in trade.partials],
            "zone": asdict(s.zone) if s.zone else None,
            "breakdown": s.breakdown.as_dict(),
            "reason": s.reason,
        }
        self.execute(
            "INSERT OR REPLACE INTO trades VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade.trade_id, s.setup_id, mode.value, symbol, s.model.value,
             s.direction.value, trade.status.value, s.grade.value, s.score,
             trade.entry_time.isoformat() if trade.entry_time else None,
             trade.entry_price, trade.initial_stop, trade.stop_price,
             trade.tp1, trade.tp2, trade.volume, trade.initial_volume,
             trade.risk_fraction, trade.risk_money,
             trade.exit_time.isoformat() if trade.exit_time else None,
             trade.exit_price,
             trade.exit_reason.value if trade.exit_reason else None,
             trade.profit, trade.commission, trade.r_multiple(),
             trade.mfe, trade.mae, trade.bars_open, trade.mt5_ticket,
             s.session.value, s.regime.value,
             json.dumps(s.tf_plan.as_dict()), s.spread_points, s.atr,
             BOT_VERSION, json.dumps(details, default=str)))

    def journal_error(self, component: str, message: str,
                      tb: str = "", level: str = "ERROR") -> None:
        self.execute("INSERT INTO errors(time,level,component,message,tb) "
                     "VALUES (?,?,?,?,?)",
                     (utcnow().isoformat(), level, component, message, tb))

    def heartbeat(self, mode: Mode, equity: float, open_trades: int,
                  lock: str, note: str = "") -> None:
        self.execute("INSERT INTO heartbeats(time,mode,equity,open_trades,lock,note) "
                     "VALUES (?,?,?,?,?,?)",
                     (utcnow().isoformat(), mode.value, equity, open_trades,
                      lock, note))

    def record_config(self, cfg: Config, note: str) -> None:
        self.execute("INSERT INTO config_history(time,version,config_hash,config_json,note) "
                     "VALUES (?,?,?,?,?)",
                     (utcnow().isoformat(), BOT_VERSION, cfg.config_hash(),
                      json.dumps(cfg.public_dict(), default=str), note))

    def record_param_change(self, name: str, old: Any, new: Any,
                            evidence: str) -> None:
        self.execute("INSERT INTO param_versions(time,name,old_value,new_value,evidence) "
                     "VALUES (?,?,?,?,?)",
                     (utcnow().isoformat(), name, json.dumps(old, default=str),
                      json.dumps(new, default=str), evidence))

    def upsert_daily(self, day: str, start_equity: float, end_equity: float,
                     realised: float, trades: int, wins: int, losses: int,
                     max_dd: float, locked: str) -> None:
        self.execute(
            "INSERT INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET end_equity=excluded.end_equity, "
            "realised=excluded.realised, trades=excluded.trades, "
            "wins=excluded.wins, losses=excluded.losses, "
            "max_drawdown=excluded.max_drawdown, locked=excluded.locked",
            (day, start_equity, end_equity, realised, trades, wins, losses,
             max_dd, locked))

    def record_backtest(self, run_id: str, data_desc: str, cfg_hash: str,
                        start: str, end: str, metrics: Dict[str, Any]) -> None:
        self.execute(
            "INSERT OR REPLACE INTO backtest_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, utcnow().isoformat(), data_desc, cfg_hash, start, end,
             metrics.get("trades", 0), metrics.get("net_return", 0.0),
             metrics.get("profit_factor", 0.0), metrics.get("expectancy_r", 0.0),
             metrics.get("max_drawdown", 0.0), json.dumps(metrics, default=str)))

    def close(self) -> None:
        with self._lock:
            self.conn.close()


# ===========================================================================
# SECTION 7 — SWING DETECTION (fractal, confirmation-delayed, no repaint)
# ===========================================================================

class SwingDetector:
    """Objective fractal swings.

    A swing HIGH at index i requires:  high[i] > high[i-k] for k=1..left AND
    high[i] >= high[i+k] with at least one strict > for k=1..right.
    The swing is only CONFIRMED once candle i+right has closed, so a swing
    can never repaint: detection at candle index j only reports swings with
    confirmed_index <= j."""

    def __init__(self, left: int, right: int):
        self.left = left
        self.right = right

    def detect(self, candles: Sequence[Candle]) -> List[SwingPoint]:
        n = len(candles)
        out: List[SwingPoint] = []
        if n < self.left + self.right + 1:
            return out
        for i in range(self.left, n - self.right):
            c = candles[i]
            is_high = all(c.high > candles[i - k].high for k in range(1, self.left + 1)) \
                and all(c.high >= candles[i + k].high for k in range(1, self.right + 1)) \
                and any(c.high > candles[i + k].high for k in range(1, self.right + 1))
            is_low = all(c.low < candles[i - k].low for k in range(1, self.left + 1)) \
                and all(c.low <= candles[i + k].low for k in range(1, self.right + 1)) \
                and any(c.low < candles[i + k].low for k in range(1, self.right + 1))
            if is_high:
                out.append(SwingPoint(i, c.time, c.high, SwingKind.HIGH,
                                      confirmed_index=i + self.right))
            if is_low:
                out.append(SwingPoint(i, c.time, c.low, SwingKind.LOW,
                                      confirmed_index=i + self.right))
        return out

    @staticmethod
    def alternating(swings: Sequence[SwingPoint]) -> List[SwingPoint]:
        """Reduce to a strictly alternating high/low sequence, keeping the
        more extreme point when two of the same kind are adjacent."""
        out: List[SwingPoint] = []
        for s in sorted(swings, key=lambda x: x.index):
            if not out or out[-1].kind != s.kind:
                out.append(s)
            else:
                last = out[-1]
                if s.kind == SwingKind.HIGH and s.price >= last.price:
                    out[-1] = s
                elif s.kind == SwingKind.LOW and s.price <= last.price:
                    out[-1] = s
        return out


# ===========================================================================
# SECTION 8 — MARKET STRUCTURE (trend, BOS, CHoCH, MSS, premium/discount)
# ===========================================================================

@dataclass
class StructureState:
    trend: TrendState
    swings: List[SwingPoint]
    events: List[StructureEvent]
    dealing_range: Optional[DealingRange]
    last_confirmed_high: Optional[SwingPoint]
    last_confirmed_low: Optional[SwingPoint]
    protected_low: Optional[SwingPoint]     # last HL that must hold (bull)
    protected_high: Optional[SwingPoint]    # last LH that must hold (bear)


class StructureAnalyzer:
    """Objective market-structure engine.

    Definitions (all on COMPLETED candles, close-based by default):
      * trend BULLISH  : last two alternating swing highs form HH and last
                         two swing lows form HL.
      * trend BEARISH  : mirrored (LH + LL).
      * BOS            : close beyond the most recent confirmed swing extreme
                         IN the direction of the current trend (continuation).
      * CHoCH          : first close beyond the protected opposing swing
                         AGAINST the current trend.
      * MSS            : a CHoCH whose breaking candle shows displacement
                         and/or the move swept liquidity first — a graded,
                         stronger shift.
    """

    def __init__(self, cfg: Config, left: Optional[int] = None,
                 right: Optional[int] = None):
        self.cfg = cfg
        self.detector = SwingDetector(left or cfg.swing_left,
                                      right or cfg.swing_right)

    def analyze(self, candles: Sequence[Candle],
                atr: Optional[List[float]] = None) -> StructureState:
        cfg = self.cfg
        n = len(candles)
        atr = atr or atr_series(candles, cfg.atr_period)
        raw = self.detector.detect(candles)
        swings = SwingDetector.alternating(raw)
        events: List[StructureEvent] = []
        trend = TrendState.UNDEFINED
        last_high: Optional[SwingPoint] = None
        last_low: Optional[SwingPoint] = None
        protected_low: Optional[SwingPoint] = None
        protected_high: Optional[SwingPoint] = None
        pending_break_high: Optional[SwingPoint] = None  # level to watch above
        pending_break_low: Optional[SwingPoint] = None
        swing_iter = 0

        for i in range(n):
            c = candles[i]
            # 1. absorb any swings that confirm at this candle
            while swing_iter < len(swings) and swings[swing_iter].confirmed_index <= i:
                s = swings[swing_iter]
                if s.kind == SwingKind.HIGH:
                    prev_high = last_high
                    last_high = s
                    pending_break_high = s
                    if trend == TrendState.BULLISH and prev_high:
                        pass  # HH tracked via break events
                else:
                    prev_low = last_low
                    last_low = s
                    pending_break_low = s
                # trend from the last 4 alternating swings
                trend = self._classify_trend(swings[:swing_iter + 1], trend)
                if trend == TrendState.BULLISH and s.kind == SwingKind.LOW:
                    protected_low = s
                if trend == TrendState.BEARISH and s.kind == SwingKind.HIGH:
                    protected_high = s
                swing_iter += 1

            # 2. break detection on this completed candle
            ref_price_up = c.close if cfg.bos_use_close else c.high
            ref_price_dn = c.close if cfg.bos_use_close else c.low
            a = atr[i] if i < len(atr) else 0.0
            disp = is_displacement(c, a, cfg)

            if pending_break_high and ref_price_up > pending_break_high.price:
                kind = self._event_kind(Direction.LONG, trend)
                ev = StructureEvent(kind, Direction.LONG, i, c.time,
                                    pending_break_high.price,
                                    pending_break_high, displacement=disp)
                if kind == StructureEventKind.CHOCH and disp:
                    ev.kind = StructureEventKind.MSS
                events.append(ev)
                if kind != StructureEventKind.BOS:
                    trend = TrendState.BULLISH
                    protected_low = last_low
                pending_break_high = None
            if pending_break_low and ref_price_dn < pending_break_low.price:
                kind = self._event_kind(Direction.SHORT, trend)
                ev = StructureEvent(kind, Direction.SHORT, i, c.time,
                                    pending_break_low.price,
                                    pending_break_low, displacement=disp)
                if kind == StructureEventKind.CHOCH and disp:
                    ev.kind = StructureEventKind.MSS
                events.append(ev)
                if kind != StructureEventKind.BOS:
                    trend = TrendState.BEARISH
                    protected_high = last_high
                pending_break_low = None

        dealing = self._dealing_range(swings)
        return StructureState(trend=trend, swings=swings, events=events,
                              dealing_range=dealing,
                              last_confirmed_high=last_high,
                              last_confirmed_low=last_low,
                              protected_low=protected_low,
                              protected_high=protected_high)

    @staticmethod
    def _event_kind(direction: Direction, trend: TrendState) -> StructureEventKind:
        if trend == TrendState.UNDEFINED or trend == TrendState.RANGING:
            return StructureEventKind.BOS
        if direction == Direction.LONG:
            return (StructureEventKind.BOS if trend == TrendState.BULLISH
                    else StructureEventKind.CHOCH)
        return (StructureEventKind.BOS if trend == TrendState.BEARISH
                else StructureEventKind.CHOCH)

    @staticmethod
    def _classify_trend(swings: Sequence[SwingPoint],
                        current: TrendState) -> TrendState:
        highs = [s for s in swings if s.kind == SwingKind.HIGH][-2:]
        lows = [s for s in swings if s.kind == SwingKind.LOW][-2:]
        if len(highs) < 2 or len(lows) < 2:
            return current if current != TrendState.UNDEFINED else TrendState.UNDEFINED
        hh = highs[1].price > highs[0].price
        hl = lows[1].price > lows[0].price
        lh = highs[1].price < highs[0].price
        ll = lows[1].price < lows[0].price
        if hh and hl:
            return TrendState.BULLISH
        if lh and ll:
            return TrendState.BEARISH
        return TrendState.RANGING

    @staticmethod
    def _dealing_range(swings: Sequence[SwingPoint]) -> Optional[DealingRange]:
        """Dealing range = most recent significant swing low <-> swing high
        (last 10 alternating swings, take extremes)."""
        recent = swings[-10:]
        highs = [s for s in recent if s.kind == SwingKind.HIGH]
        lows = [s for s in recent if s.kind == SwingKind.LOW]
        if not highs or not lows:
            return None
        hi = max(highs, key=lambda s: s.price)
        lo = min(lows, key=lambda s: s.price)
        if hi.price <= lo.price:
            return None
        return DealingRange(low=lo.price, high=hi.price,
                            low_time=lo.time, high_time=hi.time)

    def premium_discount(self, state: StructureState,
                         price: float) -> Tuple[str, float]:
        """Return ('PREMIUM'|'DISCOUNT'|'EQUILIBRIUM', position 0..1)."""
        if not state.dealing_range:
            return "EQUILIBRIUM", 0.5
        pos = state.dealing_range.position_of(price)
        buf = self.cfg.premium_discount_buffer
        if pos > 0.5 + buf:
            return "PREMIUM", pos
        if pos < 0.5 - buf:
            return "DISCOUNT", pos
        return "EQUILIBRIUM", pos

    @staticmethod
    def last_event(state: StructureState,
                   kinds: Tuple[StructureEventKind, ...],
                   direction: Optional[Direction] = None,
                   since_index: int = 0) -> Optional[StructureEvent]:
        for ev in reversed(state.events):
            if ev.index < since_index:
                return None
            if ev.kind in kinds and (direction is None or ev.direction == direction):
                return ev
        return None


# ===========================================================================
# SECTION 9 — LIQUIDITY DETECTION
# ===========================================================================

class LiquidityDetector:
    """Detects resting liquidity and objective sweeps.

    Sweep definition (objective): price TRADES THROUGH a recognised level
    (high above BSL / low below SSL) AND EITHER the candle closes back on
    the original side of the level OR a displacement candle moves away
    within `sweep_confirm_candles`. A wick through alone is NOT a sweep."""

    SWEEP_CONFIRM_CANDLES = 2

    def __init__(self, cfg: Config):
        self.cfg = cfg

    # -- level construction --------------------------------------------------
    def detect_levels(self, candles: Sequence[Candle],
                      swings: Sequence[SwingPoint],
                      session_marks: Optional[Dict[str, float]] = None,
                      ) -> List[LiquidityLevel]:
        levels: List[LiquidityLevel] = []
        atr = atr_at(candles, self.cfg.atr_period)
        tol = self.cfg.eq_level_atr_tol * atr if atr > 0 else 0.0

        highs = [s for s in swings if s.kind == SwingKind.HIGH]
        lows = [s for s in swings if s.kind == SwingKind.LOW]

        # equal highs / equal lows (>=2 swings within tolerance)
        levels += self._equal_clusters(highs, tol, True)
        levels += self._equal_clusters(lows, tol, False)

        # individual recent swing highs/lows (last 8 each)
        for s in highs[-8:]:
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.SWING_HIGH,
                                         s.price, s.time, buy_side=True))
        for s in lows[-8:]:
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.SWING_LOW,
                                         s.price, s.time, buy_side=False))

        # previous day / week highs & lows from resampled completed periods
        d1 = resample(candles, Timeframe.D1)
        if len(d1) >= 2:
            pd_ = d1[-2]
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.PDH,
                                         pd_.high, pd_.time, buy_side=True))
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.PDL,
                                         pd_.low, pd_.time, buy_side=False))
        w1 = resample(candles, Timeframe.W1)
        if len(w1) >= 2:
            pw = w1[-2]
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.PWH,
                                         pw.high, pw.time, buy_side=True))
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.PWL,
                                         pw.low, pw.time, buy_side=False))

        # session highs/lows supplied by SessionManager
        if session_marks:
            t = candles[-1].time if candles else utcnow()
            for name, price in session_marks.items():
                if price is None:
                    continue
                buy_side = name.endswith("_high")
                levels.append(LiquidityLevel(
                    new_id("liq"),
                    LiquidityKind.SESSION_HIGH if buy_side else LiquidityKind.SESSION_LOW,
                    price, t, buy_side=buy_side))
        return levels

    def _equal_clusters(self, swings: Sequence[SwingPoint], tol: float,
                        buy_side: bool) -> List[LiquidityLevel]:
        out: List[LiquidityLevel] = []
        if tol <= 0 or len(swings) < 2:
            return out
        used: set = set()
        pts = list(swings[-12:])
        for i in range(len(pts)):
            if i in used:
                continue
            cluster = [pts[i]]
            for j in range(i + 1, len(pts)):
                if j in used:
                    continue
                if abs(pts[j].price - pts[i].price) <= tol:
                    cluster.append(pts[j])
                    used.add(j)
            if len(cluster) >= 2:
                used.add(i)
                # stops rest just beyond the extreme of the cluster
                price = (max if buy_side else min)(p.price for p in cluster)
                kind = LiquidityKind.EQUAL_HIGHS if buy_side else LiquidityKind.EQUAL_LOWS
                out.append(LiquidityLevel(
                    new_id("liq"), kind, price, cluster[-1].time,
                    buy_side=buy_side,
                    member_prices=tuple(p.price for p in cluster)))
        return out

    # -- state updates & sweep detection --------------------------------------
    def update_states(self, levels: List[LiquidityLevel],
                      candles: Sequence[Candle],
                      atr: Optional[List[float]] = None) -> List[SweepEvent]:
        """Walk candles chronologically, updating level states.
        Returns the sweep events found (most recent last)."""
        cfg = self.cfg
        atr = atr or atr_series(candles, cfg.atr_period)
        sweeps: List[SweepEvent] = []
        for lvl in levels:
            if lvl.state != LiquidityState.UNTOUCHED:
                continue
            # only consider candles after the level existed
            for i, c in enumerate(candles):
                if c.time <= lvl.time:
                    continue
                pierced = c.high > lvl.price if lvl.buy_side else c.low < lvl.price
                if not pierced:
                    continue
                closed_back = c.close < lvl.price if lvl.buy_side else c.close > lvl.price
                displaced = False
                # displacement away within confirm window
                for k in range(i, min(i + self.SWEEP_CONFIRM_CANDLES + 1, len(candles))):
                    ck = candles[k]
                    a = atr[k] if k < len(atr) else 0.0
                    if not is_displacement(ck, a, cfg):
                        continue
                    if lvl.buy_side and ck.bearish and ck.close < lvl.price:
                        displaced = True
                        break
                    if not lvl.buy_side and ck.bullish and ck.close > lvl.price:
                        displaced = True
                        break
                extreme = c.high if lvl.buy_side else c.low
                ev = SweepEvent(level=lvl, index=i, time=c.time,
                                extreme=extreme, closed_back=closed_back,
                                displaced_away=displaced)
                if ev.valid:
                    lvl.state = LiquidityState.SWEPT
                    lvl.swept_time = c.time
                    sweeps.append(ev)
                else:
                    # traded through and stayed beyond => level consumed
                    if (lvl.buy_side and c.close > lvl.price) or \
                       (not lvl.buy_side and c.close < lvl.price):
                        lvl.state = LiquidityState.INVALIDATED
                break
        sweeps.sort(key=lambda s: s.index)
        return sweeps

    @staticmethod
    def nearest_target(levels: Sequence[LiquidityLevel], price: float,
                       direction: Direction) -> Optional[LiquidityLevel]:
        """Nearest untouched opposing liquidity in the trade direction."""
        if direction == Direction.LONG:
            cands = [l for l in levels if l.buy_side and l.price > price
                     and l.state == LiquidityState.UNTOUCHED]
            return min(cands, key=lambda l: l.price - price) if cands else None
        cands = [l for l in levels if not l.buy_side and l.price < price
                 and l.state == LiquidityState.UNTOUCHED]
        return min(cands, key=lambda l: price - l.price) if cands else None

    @staticmethod
    def targets_beyond(levels: Sequence[LiquidityLevel], price: float,
                       direction: Direction, count: int = 3) -> List[LiquidityLevel]:
        if direction == Direction.LONG:
            cands = sorted([l for l in levels if l.buy_side and l.price > price
                            and l.state == LiquidityState.UNTOUCHED],
                           key=lambda l: l.price)
        else:
            cands = sorted([l for l in levels if not l.buy_side and l.price < price
                            and l.state == LiquidityState.UNTOUCHED],
                           key=lambda l: -l.price)
        return cands[:count]


# ===========================================================================
# SECTION 10 — SUPPLY & DEMAND ZONES
# ===========================================================================

class SupplyDemandDetector:
    """Transparent supply/demand zone engine (no private indicators).

    A zone = leg-in -> base -> leg-out:
      * base: 1..zone_base_max_candles consecutive candles whose bodies are
        all <= zone_base_body_atr * ATR.
      * leg-out: the first candle after the base shows displacement; its
        direction defines SUPPLY (down) or DEMAND (up).
      * leg-in direction + leg-out direction give RBD/DBR/DBD/RBR.
    Boundaries: distal = extreme of the base range, proximal = the base
    body edge nearest to the departure. Invalidation = close through distal.
    """

    def __init__(self, cfg: Config, timeframe: Timeframe):
        self.cfg = cfg
        self.tf = timeframe

    def detect(self, candles: Sequence[Candle],
               structure: Optional[StructureState] = None) -> List[Zone]:
        cfg = self.cfg
        n = len(candles)
        if n < cfg.atr_period + 6:
            return []
        atr = atr_series(candles, cfg.atr_period)
        zones: List[Zone] = []
        i = cfg.atr_period
        while i < n - 1:
            a = atr[i]
            if a <= 0:
                i += 1
                continue
            # find base start: candle i small-bodied
            if candles[i].body > cfg.zone_base_body_atr * a:
                i += 1
                continue
            base_start = i
            base_end = i
            while (base_end + 1 < n - 1
                   and base_end - base_start + 1 < cfg.zone_base_max_candles
                   and candles[base_end + 1].body <= cfg.zone_base_body_atr * atr[base_end + 1]):
                base_end += 1
            leg_out_idx = base_end + 1
            if leg_out_idx >= n:
                break
            out_c = candles[leg_out_idx]
            a_out = atr[leg_out_idx]
            if not is_displacement(out_c, a_out, cfg):
                i = base_end + 1
                continue
            # leg-in direction: net move of the 3 candles before the base
            pre = candles[max(0, base_start - 3):base_start]
            leg_in_up = bool(pre) and pre[-1].close >= pre[0].open
            leg_out_up = out_c.bullish
            base = candles[base_start:base_end + 1]
            base_high = max(c.high for c in base)
            base_low = min(c.low for c in base)
            body_high = max(max(c.open, c.close) for c in base)
            body_low = min(min(c.open, c.close) for c in base)
            if leg_out_up:
                kind = ZoneKind.DEMAND
                pattern = ZonePattern.DBR if not leg_in_up else ZonePattern.RBR
                upper, lower = body_high, base_low
            else:
                kind = ZoneKind.SUPPLY
                pattern = ZonePattern.RBD if leg_in_up else ZonePattern.DBD
                upper, lower = base_high, body_low
            disp_score = min(1.0, out_c.body / (2.0 * a_out)) if a_out > 0 else 0.0
            z = Zone(zone_id=new_id("zone"), kind=kind, pattern=pattern,
                     upper=upper, lower=lower, timeframe=self.tf,
                     created_time=candles[base_start].time,
                     created_index=base_start,
                     displacement_score=disp_score)
            zones.append(z)
            i = leg_out_idx + 1

        self._update_zone_states(zones, candles, structure)
        return zones

    def _update_zone_states(self, zones: List[Zone],
                            candles: Sequence[Candle],
                            structure: Optional[StructureState]) -> None:
        cfg = self.cfg
        n = len(candles)
        bos_indices = ([ev.index for ev in structure.events]
                       if structure else [])
        for z in zones:
            # BOS caused within 6 candles of the departure
            z.caused_bos = any(z.created_index < bi <= z.created_index + 8
                               for bi in bos_indices)
            touches = 0
            for j in range(z.created_index + 2, n):
                c = candles[j]
                if z.invalidated:
                    break
                if z.kind == ZoneKind.DEMAND:
                    if c.close < z.lower:
                        z.invalidated = True
                        break
                    if c.low <= z.upper:
                        touches += 1
                else:
                    if c.close > z.upper:
                        z.invalidated = True
                        break
                    if c.high >= z.lower:
                        touches += 1
            z.touches = touches
            age = n - 1 - z.created_index
            age_factor = max(0.0, 1.0 - age / cfg.zone_max_age_candles)
            touch_factor = max(0.0, 1.0 - touches / (cfg.zone_max_touches + 1))
            z.freshness = round(age_factor * touch_factor, 3)
            if touches > cfg.zone_max_touches:
                z.freshness = 0.0

    @staticmethod
    def active_zones(zones: Sequence[Zone], kind: Optional[ZoneKind] = None,
                     min_quality: float = 0.25) -> List[Zone]:
        out = [z for z in zones if not z.invalidated and z.quality() >= min_quality]
        if kind:
            out = [z for z in out if z.kind == kind]
        return out

    @staticmethod
    def zone_at_price(zones: Sequence[Zone], price: float,
                      kind: Optional[ZoneKind] = None,
                      tolerance: float = 0.0) -> Optional[Zone]:
        best: Optional[Zone] = None
        for z in SupplyDemandDetector.active_zones(zones, kind):
            if z.lower - tolerance <= price <= z.upper + tolerance:
                if best is None or z.quality() > best.quality():
                    best = z
        return best


# ===========================================================================
# SECTION 11 — FAIR VALUE GAPS
# ===========================================================================

class FVGDetector:
    """Strict three-candle FVG logic.

    Bullish FVG at middle candle i: low[i+1] > high[i-1]  (gap up)
    Bearish FVG at middle candle i: high[i+1] < low[i-1]  (gap down)
    Gap must be >= fvg_min_size_atr * ATR. FVGs are confluence only."""

    def __init__(self, cfg: Config, timeframe: Timeframe):
        self.cfg = cfg
        self.tf = timeframe

    def detect(self, candles: Sequence[Candle]) -> List[FairValueGap]:
        cfg = self.cfg
        n = len(candles)
        if n < 3:
            return []
        atr = atr_series(candles, cfg.atr_period)
        gaps: List[FairValueGap] = []
        for i in range(1, n - 1):
            a = atr[i]
            if a <= 0:
                continue
            c_prev, c_mid, c_next = candles[i - 1], candles[i], candles[i + 1]
            min_size = cfg.fvg_min_size_atr * a
            if c_next.low > c_prev.high and (c_next.low - c_prev.high) >= min_size:
                gaps.append(FairValueGap(
                    fvg_id=new_id("fvg"), direction=Direction.LONG,
                    upper=c_next.low, lower=c_prev.high, timeframe=self.tf,
                    created_time=c_mid.time, created_index=i,
                    from_displacement=is_displacement(c_mid, a, cfg)))
            elif c_next.high < c_prev.low and (c_prev.low - c_next.high) >= min_size:
                gaps.append(FairValueGap(
                    fvg_id=new_id("fvg"), direction=Direction.SHORT,
                    upper=c_prev.low, lower=c_next.high, timeframe=self.tf,
                    created_time=c_mid.time, created_index=i,
                    from_displacement=is_displacement(c_mid, a, cfg)))
        self._update_fill_states(gaps, candles)
        return gaps

    @staticmethod
    def _update_fill_states(gaps: List[FairValueGap],
                            candles: Sequence[Candle]) -> None:
        n = len(candles)
        for g in gaps:
            worst_penetration = 0.0
            for j in range(g.created_index + 2, n):
                c = candles[j]
                if g.direction == Direction.LONG:
                    if c.low <= g.lower:
                        worst_penetration = g.size
                        break
                    if c.low < g.upper:
                        worst_penetration = max(worst_penetration, g.upper - c.low)
                else:
                    if c.high >= g.upper:
                        worst_penetration = g.size
                        break
                    if c.high > g.lower:
                        worst_penetration = max(worst_penetration, c.high - g.lower)
            if g.size <= 0:
                g.state = FVGState.MITIGATED
                g.fill_fraction = 1.0
                continue
            g.fill_fraction = min(1.0, worst_penetration / g.size)
            if g.fill_fraction >= 1.0:
                g.state = FVGState.MITIGATED
            elif g.fill_fraction > 0.0:
                g.state = FVGState.PARTIAL
            else:
                g.state = FVGState.UNFILLED

    @staticmethod
    def usable(gaps: Sequence[FairValueGap],
               direction: Optional[Direction] = None) -> List[FairValueGap]:
        out = [g for g in gaps if g.state != FVGState.MITIGATED]
        if direction:
            out = [g for g in out if g.direction == direction]
        return out


# ===========================================================================
# SECTION 12 — ORDER BLOCKS
# ===========================================================================

class OrderBlockDetector:
    """Order block = the LAST opposite-coloured candle immediately before a
    displacement move that (a) produced BOS/CHoCH/MSS or (b) swept
    liquidity. Plain opposite candles are NOT order blocks."""

    def __init__(self, cfg: Config, timeframe: Timeframe):
        self.cfg = cfg
        self.tf = timeframe

    def detect(self, candles: Sequence[Candle],
               structure: Optional[StructureState] = None,
               sweeps: Optional[Sequence[SweepEvent]] = None) -> List[OrderBlock]:
        cfg = self.cfg
        n = len(candles)
        if n < cfg.atr_period + 3:
            return []
        atr = atr_series(candles, cfg.atr_period)
        event_idx = {ev.index for ev in (structure.events if structure else [])}
        sweep_idx = {sv.index for sv in (sweeps or [])}
        blocks: List[OrderBlock] = []
        for i in range(cfg.atr_period, n):
            c = candles[i]
            a = atr[i]
            if not is_displacement(c, a, cfg):
                continue
            # structural / liquidity link within a short forward window
            linked_ev = next((ev for ev in (structure.events if structure else [])
                              if i <= ev.index <= i + 3), None)
            linked_sweep = any(i - 2 <= si <= i + 1 for si in sweep_idx)
            if cfg.ob_require_structure_link and not linked_ev and not linked_sweep:
                continue
            # walk back for the last opposite-coloured candle
            ob_idx = None
            for k in range(i - 1, max(0, i - 4) - 1, -1):
                if c.bullish and candles[k].bearish:
                    ob_idx = k
                    break
                if c.bearish and candles[k].bullish:
                    ob_idx = k
                    break
            if ob_idx is None:
                continue
            ob_c = candles[ob_idx]
            direction = Direction.LONG if c.bullish else Direction.SHORT
            blocks.append(OrderBlock(
                ob_id=new_id("ob"), direction=direction,
                upper=ob_c.high, lower=ob_c.low, timeframe=self.tf,
                created_time=ob_c.time, created_index=ob_idx,
                linked_structure=linked_ev.kind if linked_ev else None,
                linked_sweep=linked_sweep))
        # deduplicate by index (keep the last classification)
        seen: Dict[int, OrderBlock] = {}
        for b in blocks:
            seen[b.created_index] = b
        blocks = sorted(seen.values(), key=lambda b: b.created_index)
        self._update_states(blocks, candles)
        return blocks

    @staticmethod
    def _update_states(blocks: List[OrderBlock],
                       candles: Sequence[Candle]) -> None:
        n = len(candles)
        for b in blocks:
            mitigations = 0
            for j in range(b.created_index + 2, n):
                c = candles[j]
                if b.direction == Direction.LONG:
                    if c.close < b.lower:
                        b.invalidated = True
                        break
                    if c.low <= b.upper:
                        mitigations += 1
                else:
                    if c.close > b.upper:
                        b.invalidated = True
                        break
                    if c.high >= b.lower:
                        mitigations += 1
            b.mitigations = mitigations
            b.freshness = max(0.0, 1.0 - mitigations / 3.0)
            if b.invalidated:
                b.freshness = 0.0

    @staticmethod
    def usable(blocks: Sequence[OrderBlock],
               direction: Optional[Direction] = None,
               min_freshness: float = 0.3) -> List[OrderBlock]:
        out = [b for b in blocks if not b.invalidated and b.freshness >= min_freshness]
        if direction:
            out = [b for b in out if b.direction == direction]
        return out


# ===========================================================================
# SECTION 13 — MARKET REGIME CLASSIFICATION
# ===========================================================================

class MarketRegimeDetector:
    """Objective regime classification from structure + volatility inputs."""

    ATR_HISTORY = 200
    EFF_LOOKBACK = 20
    RANGE_LOOKBACK = 40

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def classify(self, candles: Sequence[Candle],
                 structure: StructureState,
                 spread_points: float = 0.0,
                 point: float = 0.01,
                 news_active: bool = False) -> RegimeReading:
        cfg = self.cfg
        if len(candles) < cfg.atr_period + self.EFF_LOOKBACK + 5:
            return RegimeReading(Regime.UNSAFE, TrendState.UNDEFINED, 0.0,
                                 0.0, 0.5, 0.0, "insufficient candle history")
        atr_all = atr_series(candles, cfg.atr_period)
        atr_now = atr_all[-1]
        hist = atr_all[-self.ATR_HISTORY:]
        atr_pct = percentile_rank(hist, atr_now)
        eff = efficiency_ratio(candles, self.EFF_LOOKBACK)
        roc = rate_of_change(candles, self.EFF_LOOKBACK)
        trend = structure.trend

        # spread safety first
        if spread_points > cfg.max_spread_points:
            return RegimeReading(Regime.ABNORMAL_SPREAD, trend, 0.9, atr_now,
                                 atr_pct, eff,
                                 f"spread {spread_points:.0f}pt > max {cfg.max_spread_points:.0f}pt")
        if news_active:
            return RegimeReading(Regime.NEWS_VOLATILITY, trend, 0.9, atr_now,
                                 atr_pct, eff, "news window active")

        # abnormal candle: last completed candle is a huge outlier
        last = candles[-1]
        if atr_now > 0 and last.range > 4.0 * atr_now:
            return RegimeReading(Regime.NEWS_VOLATILITY, trend, 0.8, atr_now,
                                 atr_pct, eff,
                                 f"outlier candle range {last.range:.2f} > 4x ATR")

        window = candles[-self.RANGE_LOOKBACK:]
        width = max(c.high for c in window) - min(c.low for c in window)
        width_atr = width / atr_now if atr_now > 0 else 0.0

        recent_events = [ev for ev in structure.events
                         if ev.index >= len(candles) - self.EFF_LOOKBACK]
        recent_choch = [ev for ev in recent_events
                        if ev.kind in (StructureEventKind.CHOCH, StructureEventKind.MSS)]
        disp_count = sum(1 for i in range(len(candles) - 6, len(candles))
                         if i >= 0 and is_displacement(candles[i], atr_all[i], cfg))

        # compression: low vol percentile + narrow range
        if atr_pct <= cfg.low_vol_atr_percentile and width_atr < 8.0 and eff < 0.25:
            return RegimeReading(Regime.COMPRESSION, trend, 0.7, atr_now,
                                 atr_pct, eff,
                                 f"ATR pct {atr_pct:.2f}, width {width_atr:.1f} ATR, eff {eff:.2f}")
        # expansion: vol percentile spiking + displacement burst
        if atr_pct >= cfg.high_vol_atr_percentile and disp_count >= 2:
            return RegimeReading(Regime.EXPANSION, trend, 0.7, atr_now,
                                 atr_pct, eff,
                                 f"ATR pct {atr_pct:.2f} with {disp_count} displacement candles")
        # reversal attempt: fresh counter-trend CHoCH/MSS
        if recent_choch:
            return RegimeReading(Regime.REVERSAL_ATTEMPT, trend, 0.6, atr_now,
                                 atr_pct, eff,
                                 f"recent {recent_choch[-1].kind.value} against prior trend")
        if trend == TrendState.BULLISH:
            strong = eff >= 0.35 and roc > 0
            return RegimeReading(Regime.STRONG_BULL if strong else Regime.WEAK_BULL,
                                 trend, 0.7 if strong else 0.55, atr_now,
                                 atr_pct, eff,
                                 f"bullish structure, eff {eff:.2f}, roc {roc:+.4f}")
        if trend == TrendState.BEARISH:
            strong = eff >= 0.35 and roc < 0
            return RegimeReading(Regime.STRONG_BEAR if strong else Regime.WEAK_BEAR,
                                 trend, 0.7 if strong else 0.55, atr_now,
                                 atr_pct, eff,
                                 f"bearish structure, eff {eff:.2f}, roc {roc:+.4f}")
        return RegimeReading(Regime.RANGE, trend, 0.6, atr_now, atr_pct, eff,
                             f"no directional structure, eff {eff:.2f}, width {width_atr:.1f} ATR")


# ===========================================================================
# SECTION 14 — ADAPTIVE TIMEFRAME ENGINE
# ===========================================================================

class TimeframeSelector:
    """Chooses the timeframe role combination for current conditions.

    Combos (bias / structure / decision / entry / management):
      C1: D1 H4 H1 M15 M15   — slow, high-vol or macro-driven conditions
      C2: H4 H1 M15 M15 M15  — standard swing/intraday hybrid
      C3: H1 M15 M15 M5 M5   — active intraday, normal vol, good sessions
      C4: H1 M15 M5  M1 M5   — precision only when M1 is clean & spread low
      C5: M30 M5 M5  M1 M5   — fast intraday, only in strong clean sessions
    Never chooses a combo because it would produce more signals; the driver
    is data quality, volatility, spread and session."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def select(self, candle_counts: Dict[Timeframe, int],
               atr_percentile: float, spread_points: float,
               atr_points: float, session: SessionName,
               regime: Regime, m1_efficiency: float) -> TimeframePlan:
        cfg = self.cfg
        have = {tf for tf, n in candle_counts.items()
                if n >= cfg.min_candles_required}
        reasons: List[str] = []

        spread_ratio = spread_points / atr_points if atr_points > 0 else 1.0
        m1_ok = (Timeframe.M1 in have
                 and m1_efficiency >= cfg.m1_noise_efficiency_max
                 and spread_ratio < 0.25
                 and session in (SessionName.LONDON, SessionName.NEW_YORK,
                                 SessionName.OVERLAP))
        if Timeframe.M1 in have and not m1_ok:
            reasons.append(f"M1 excluded (eff {m1_efficiency:.2f}, "
                           f"spread/ATR {spread_ratio:.2f}, session {session.value})")

        high_vol = atr_percentile >= cfg.high_vol_atr_percentile
        low_vol = atr_percentile <= cfg.low_vol_atr_percentile
        good_session = session in (SessionName.LONDON, SessionName.NEW_YORK,
                                   SessionName.OVERLAP)

        def plan(b, s, d, e, m, why) -> TimeframePlan:
            reasons.append(why)
            return TimeframePlan(b, s, d, e, m, "; ".join(reasons))

        if high_vol or regime in (Regime.NEWS_VOLATILITY, Regime.EXPANSION):
            # slow everything down; require HTF anchoring
            if Timeframe.D1 in have and Timeframe.H4 in have:
                return plan(Timeframe.D1, Timeframe.H4, Timeframe.H1,
                            Timeframe.M15, Timeframe.M15,
                            f"high volatility (ATR pct {atr_percentile:.2f}) -> "
                            "C1 D1/H4/H1/M15: stronger confirmation, higher TFs")
            return plan(Timeframe.H4, Timeframe.H1, Timeframe.M15,
                        Timeframe.M15, Timeframe.M15,
                        "high volatility with limited D1 history -> C2")
        if low_vol or regime == Regime.COMPRESSION:
            return plan(Timeframe.H4, Timeframe.H1, Timeframe.M15,
                        Timeframe.M15, Timeframe.M15,
                        f"low volatility (ATR pct {atr_percentile:.2f}) -> C2 "
                        "M15 structure, no forced entries")
        if good_session and m1_ok and regime in (Regime.STRONG_BULL,
                                                 Regime.STRONG_BEAR):
            return plan(Timeframe.H1, Timeframe.M15, Timeframe.M5,
                        Timeframe.M1, Timeframe.M5,
                        "clean strong trend in liquid session with usable M1 "
                        "-> C4 precision combo")
        if good_session and Timeframe.M5 in have:
            return plan(Timeframe.H1, Timeframe.M15, Timeframe.M15,
                        Timeframe.M5, Timeframe.M5,
                        f"normal conditions in {session.value} -> C3 "
                        "H1 bias, M15 decision, M5 execution")
        return plan(Timeframe.H4, Timeframe.H1, Timeframe.M15,
                    Timeframe.M15, Timeframe.M15,
                    f"quiet/off session ({session.value}) -> C2 conservative")


# ===========================================================================
# SECTION 15 — SESSION MANAGER
# ===========================================================================

@dataclass
class SessionRange:
    name: SessionName
    day: date
    high: Optional[float] = None
    low: Optional[float] = None
    swept_high: bool = False
    swept_low: bool = False


class SessionManager:
    """UTC-internal session tracking with user-timezone display support.
    Session windows are configured in UTC hours; the user timezone
    (Europe/Madrid) is only for reporting. DST for the user's local wall
    clock is handled by zoneinfo automatically at display time."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        try:
            self.user_tz = ZoneInfo(cfg.user_timezone)
        except Exception:
            self.user_tz = UTC
        self._ranges: Dict[Tuple[date, SessionName], SessionRange] = {}

    def session_at(self, t: datetime) -> SessionName:
        h = t.astimezone(UTC).hour
        c = self.cfg
        in_london = c.london_start_utc <= h < c.london_end_utc
        in_ny = c.ny_start_utc <= h < c.ny_end_utc
        if in_london and in_ny:
            return SessionName.OVERLAP
        if in_london:
            return SessionName.LONDON
        if in_ny:
            return SessionName.NEW_YORK
        if c.asia_start_utc <= h < c.asia_end_utc:
            return SessionName.ASIA
        return SessionName.OFF_HOURS

    def to_user_time(self, t: datetime) -> datetime:
        return t.astimezone(self.user_tz)

    def update_ranges(self, candle: Candle) -> None:
        """Feed each completed base candle to build session highs/lows."""
        t = candle.time.astimezone(UTC)
        name = self.session_at(t)
        if name == SessionName.OFF_HOURS:
            return
        keys = [name]
        if name == SessionName.OVERLAP:
            keys = [SessionName.LONDON, SessionName.NEW_YORK]
        for key in keys:
            k = (t.date(), key)
            r = self._ranges.get(k)
            if r is None:
                r = SessionRange(key, t.date())
                self._ranges[k] = r
            r.high = candle.high if r.high is None else max(r.high, candle.high)
            r.low = candle.low if r.low is None else min(r.low, candle.low)

    def rebuild_from(self, candles: Sequence[Candle]) -> None:
        self._ranges.clear()
        for c in candles:
            self.update_ranges(c)

    def marks_for(self, day: date) -> Dict[str, Optional[float]]:
        out: Dict[str, Optional[float]] = {}
        for name in (SessionName.ASIA, SessionName.LONDON, SessionName.NEW_YORK):
            r = self._ranges.get((day, name))
            out[f"{name.value.lower()}_high"] = r.high if r else None
            out[f"{name.value.lower()}_low"] = r.low if r else None
        return out

    def asian_range(self, day: date) -> Optional[Tuple[float, float]]:
        r = self._ranges.get((day, SessionName.ASIA))
        if r and r.high is not None and r.low is not None:
            return (r.low, r.high)
        return None

    # -- entry-time gating ---------------------------------------------------
    def entry_window_check(self, t: datetime) -> Tuple[bool, str]:
        """Return (allowed, reason). UTC-based calendar rules."""
        c = self.cfg
        tu = t.astimezone(UTC)
        wd = tu.weekday()  # Mon=0 ... Sun=6
        if wd >= 5:
            return False, "weekend"
        if wd == 4 and tu.hour >= c.friday_last_entry_hour_utc:
            return False, f"late Friday (after {c.friday_last_entry_hour_utc}:00 UTC)"
        if wd == 0 and tu.hour < c.monday_first_entry_hour_utc:
            return False, "early Monday instability window"
        lo, hi = c.avoid_rollover_utc
        if lo <= tu.hour < hi:
            return False, f"daily rollover window {lo}-{hi} UTC"
        sess = self.session_at(tu)
        if sess == SessionName.OFF_HOURS:
            return False, "off-hours / illiquid"
        return True, sess.value

    def near_weekend_flat(self, t: datetime) -> bool:
        tu = t.astimezone(UTC)
        return tu.weekday() == 4 and tu.hour >= self.cfg.friday_flat_hour_utc


# ===========================================================================
# SECTION 16 — NEWS FILTER (provider interface, fail-safe)
# ===========================================================================

@dataclass
class NewsEvent:
    time: datetime
    title: str
    currency: str
    impact: str          # "HIGH" / "MEDIUM" / "LOW"


class NewsProvider:
    """Interface. Implementations must NEVER fabricate events and must
    raise/return [] on failure so the filter can fail safely."""

    name = "base"

    def fetch(self, start: datetime, end: datetime) -> List[NewsEvent]:
        raise NotImplementedError

    def healthy(self) -> bool:
        return False


class ManualBlackoutProvider(NewsProvider):
    """User-configured blackout windows:
    'YYYY-MM-DDTHH:MM/YYYY-MM-DDTHH:MM' (UTC)."""

    name = "manual"

    def __init__(self, windows: Sequence[str]):
        self.windows: List[Tuple[datetime, datetime]] = []
        for w in windows:
            try:
                a, b = w.split("/")
                t0 = datetime.fromisoformat(a).replace(tzinfo=UTC)
                t1 = datetime.fromisoformat(b).replace(tzinfo=UTC)
                if t1 > t0:
                    self.windows.append((t0, t1))
            except ValueError:
                log.warning("ignoring malformed blackout window: %s", w)

    def fetch(self, start: datetime, end: datetime) -> List[NewsEvent]:
        out = []
        for t0, t1 in self.windows:
            if t0 <= end and t1 >= start:
                out.append(NewsEvent(t0, "manual blackout", "USD", "HIGH"))
        return out

    def healthy(self) -> bool:
        return True

    def window_active(self, t: datetime) -> bool:
        return any(t0 <= t <= t1 for t0, t1 in self.windows)


class ApiNewsProvider(NewsProvider):
    """Economic-calendar provider skeleton wired for a JSON REST API.
    Requires NEWS_API_KEY and a concrete endpoint; without them it reports
    unhealthy and the filter logs that live protection is incomplete.
    The default endpoint is intentionally empty: this bot will not invent
    an unofficial scraping target."""

    name = "api"
    HIGH_IMPACT_KEYWORDS = (
        "FOMC", "FED", "FEDERAL RESERVE", "POWELL", "CPI", "CORE CPI", "PCE",
        "NONFARM", "NON-FARM", "NFP", "UNEMPLOYMENT", "PPI", "GDP",
        "RETAIL SALES", "ISM", "JOBLESS", "INTEREST RATE", "RATE DECISION")

    def __init__(self, api_key: str, endpoint: str = ""):
        self.api_key = api_key
        self.endpoint = endpoint
        self._last_ok = False

    def fetch(self, start: datetime, end: datetime) -> List[NewsEvent]:
        if not (self.api_key and self.endpoint and REQUESTS_AVAILABLE):
            self._last_ok = False
            return []
        try:
            resp = _requests.get(
                self.endpoint,
                params={"from": start.isoformat(), "to": end.isoformat()},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("news provider fetch failed: %s", exc)
            self._last_ok = False
            return []
        events: List[NewsEvent] = []
        for item in data if isinstance(data, list) else data.get("events", []):
            try:
                t = datetime.fromisoformat(str(item["time"]))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=UTC)
                events.append(NewsEvent(
                    t, str(item.get("title", "")),
                    str(item.get("currency", "")).upper(),
                    str(item.get("impact", "")).upper()))
            except (KeyError, ValueError, TypeError):
                continue
        self._last_ok = True
        return events

    def healthy(self) -> bool:
        return self._last_ok


class NewsFilter:
    """Blocks entries around high-impact USD events. Fail-safe: if no live
    provider is available it logs the gap, uses manual windows, and lets
    the spread/volatility locks act as the fallback guard."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.manual = ManualBlackoutProvider(cfg.manual_blackouts_utc)
        self.api: Optional[ApiNewsProvider] = None
        if cfg.news_api_key:
            self.api = ApiNewsProvider(cfg.news_api_key)
        else:
            log.warning("NEWS FILTER: no NEWS_API_KEY — live news protection "
                        "is INCOMPLETE; using manual blackout windows and "
                        "spread/volatility locks only")
        self._cache: List[NewsEvent] = []
        self._cache_time: Optional[datetime] = None

    def _events_near(self, t: datetime) -> List[NewsEvent]:
        events = list(self.manual.fetch(t - timedelta(hours=12),
                                        t + timedelta(hours=12)))
        if self.api:
            if (self._cache_time is None
                    or (t - self._cache_time).total_seconds() > 1800):
                fetched = self.api.fetch(t - timedelta(hours=12),
                                         t + timedelta(hours=12))
                if self.api.healthy():
                    self._cache = fetched
                    self._cache_time = t
                elif self.cfg.news_fail_safe_block_minutes > 0:
                    log.warning("news provider unreachable — fail-safe "
                                "blocking new entries")
                    return [NewsEvent(t, "provider unreachable (fail-safe)",
                                      "USD", "HIGH")]
            events += self._cache
        return events

    def blackout(self, t: datetime) -> Tuple[bool, str]:
        """(blocked, reason) for time t (UTC)."""
        c = self.cfg
        if self.manual.window_active(t):
            return True, "manual blackout window"
        for ev in self._events_near(t):
            if ev.impact != "HIGH" or ev.currency not in ("USD", "XAU", "ALL"):
                continue
            before = ev.time - timedelta(minutes=c.news_block_before_min)
            after = ev.time + timedelta(minutes=c.news_block_after_min)
            if before <= t <= after:
                return True, f"high-impact event: {ev.title or 'scheduled news'}"
        return False, ""

    def protection_complete(self) -> bool:
        return self.api is not None and self.api.healthy()


# ===========================================================================
# SECTION 17 — SETUP SCORING
# ===========================================================================

class SetupScorer:
    """0-100 scoring with a full breakdown (see spec section 14). Scores are
    functions of market evidence only — never of recent P/L or distance to
    the daily target."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def score(self,
              direction: Direction,
              htf_bias: TrendState,
              zone: Optional[Zone],
              sweep: Optional[SweepEvent],
              structure_event: Optional[StructureEvent],
              displacement: bool,
              fvg: Optional[FairValueGap],
              order_block: Optional[OrderBlock],
              pd_zone: str,                 # "PREMIUM"/"DISCOUNT"/"EQUILIBRIUM"
              session: SessionName,
              news_blocked: bool,
              news_protection_complete: bool,
              rr_tp1: float,
              target_is_liquidity: bool) -> ScoreBreakdown:
        b = ScoreBreakdown()

        # HTF alignment /15
        if (direction == Direction.LONG and htf_bias == TrendState.BULLISH) or \
           (direction == Direction.SHORT and htf_bias == TrendState.BEARISH):
            b.htf_alignment = 15.0
        elif htf_bias in (TrendState.RANGING, TrendState.UNDEFINED):
            b.htf_alignment = 8.0
        else:
            b.htf_alignment = 0.0     # countertrend

        # zone quality /15
        if zone is not None:
            b.zone_quality = round(15.0 * zone.quality(), 2)

        # liquidity sweep /15
        if sweep is not None and sweep.valid:
            pts = 9.0
            if sweep.closed_back:
                pts += 3.0
            if sweep.displaced_away:
                pts += 3.0
            b.liquidity_sweep = pts

        # structure confirmation /15
        if structure_event is not None:
            b.structure_confirmation = {
                StructureEventKind.MSS: 15.0,
                StructureEventKind.CHOCH: 12.0,
                StructureEventKind.BOS: 11.0,
            }[structure_event.kind]

        # displacement /10
        if displacement or (structure_event and structure_event.displacement):
            b.displacement = 10.0

        # FVG / OB confluence /10
        conf = 0.0
        if fvg is not None and fvg.state != FVGState.MITIGATED:
            conf += 5.0
        if order_block is not None and not order_block.invalidated:
            conf += 5.0
        b.confluence = conf

        # premium/discount /5
        if (direction == Direction.LONG and pd_zone == "DISCOUNT") or \
           (direction == Direction.SHORT and pd_zone == "PREMIUM"):
            b.premium_discount = 5.0
        elif pd_zone == "EQUILIBRIUM":
            b.premium_discount = 2.0

        # session quality /5
        b.session_quality = {
            SessionName.OVERLAP: 5.0, SessionName.LONDON: 5.0,
            SessionName.NEW_YORK: 4.0, SessionName.ASIA: 2.0,
            SessionName.OFF_HOURS: 0.0}[session]

        # news safety /5
        if news_blocked:
            b.news_safety = 0.0
        elif news_protection_complete:
            b.news_safety = 5.0
        else:
            b.news_safety = 3.0    # manual-only protection

        # target quality / net RR /5
        if rr_tp1 >= 3.0:
            b.target_quality = 5.0
        elif rr_tp1 >= 2.0:
            b.target_quality = 4.0
        elif rr_tp1 >= 1.5:
            b.target_quality = 2.0
        if target_is_liquidity and b.target_quality > 0:
            b.target_quality = min(5.0, b.target_quality + 1.0)
        return b


# ===========================================================================
# SECTION 18 — POSITION SIZING (true monetary risk from broker specs)
# ===========================================================================

@dataclass
class SizingResult:
    volume: float
    risk_money: float                 # actual risk at the rounded volume
    intended_risk_money: float
    risk_fraction_actual: float
    stop_points: float
    cost_estimate: float
    rejected: bool = False
    reason: str = ""


class PositionSizer:
    """Sizes positions from actual symbol specification. Never rounds up.
    Rejects when the minimum lot already risks more than permitted."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def size(self, spec: SymbolSpecification, equity: float,
             risk_fraction: float, entry: float, stop: float) -> SizingResult:
        cfg = self.cfg
        risk_fraction = min(risk_fraction, cfg.hard_max_risk_per_trade)
        stop_dist = abs(entry - stop)
        if stop_dist <= 0 or equity <= 0:
            return SizingResult(0, 0, 0, 0, 0, 0, True, "invalid stop/equity")
        money_per_unit = spec.money_per_price_unit_per_lot()
        if money_per_unit <= 0:
            return SizingResult(0, 0, 0, 0, 0, 0, True,
                                "symbol spec has no tick value/size")
        intended = equity * risk_fraction
        # true per-lot cost: stop distance + slippage buffer + spread + commission
        slip = cfg.slippage_buffer_points * spec.point
        spread_cost = spec.spread_points * spec.point
        loss_per_lot = (stop_dist + slip + spread_cost) * money_per_unit \
            + cfg.commission_per_lot
        if loss_per_lot <= 0:
            return SizingResult(0, 0, 0, 0, 0, 0, True, "degenerate cost model")
        raw_volume = intended / loss_per_lot
        volume = spec.round_volume_down(raw_volume)
        if volume <= 0:
            min_risk = spec.volume_min * loss_per_lot
            return SizingResult(
                0, 0, intended, 0, stop_dist / spec.point,
                cfg.commission_per_lot * spec.volume_min, True,
                f"minimum lot {spec.volume_min} would risk "
                f"{min_risk:.2f} ({min_risk / equity:.2%}) > intended "
                f"{intended:.2f} ({risk_fraction:.2%})")
        actual = volume * loss_per_lot
        # rounding down can never exceed intended risk; double-check anyway
        if actual > intended * 1.0001:
            return SizingResult(0, 0, intended, 0, stop_dist / spec.point,
                                0, True, "sizing exceeded intended risk")
        return SizingResult(
            volume=volume, risk_money=actual, intended_risk_money=intended,
            risk_fraction_actual=actual / equity,
            stop_points=stop_dist / spec.point,
            cost_estimate=cfg.commission_per_lot * volume
            + (slip + spread_cost) * money_per_unit * volume)

    def risk_fraction_for(self, grade: SetupGrade, score: float) -> float:
        """Default setup-based risk. AGGRESSIVE_MODE only lifts A+ setups,
        never above the hard cap, and never automatically."""
        cfg = self.cfg
        if grade == SetupGrade.A_PLUS:
            base = cfg.default_risk_a_plus
            if cfg.aggressive_mode:
                base = min(cfg.aggressive_risk_a_plus, cfg.hard_max_risk_per_trade)
            return base
        if grade == SetupGrade.A:
            # scale within [default_risk_a, default_risk_a_max] by score
            span = cfg.default_risk_a_max - cfg.default_risk_a
            frac = (score - 80.0) / 9.0
            return cfg.default_risk_a + span * max(0.0, min(1.0, frac))
        if grade == SetupGrade.B:
            span = cfg.default_risk_b_max - cfg.default_risk_b
            frac = (score - 70.0) / 9.0
            return cfg.default_risk_b + span * max(0.0, min(1.0, frac))
        return 0.0


# ===========================================================================
# SECTION 19 — RISK MANAGER (locks & limits; capital protection first)
# ===========================================================================

@dataclass
class DayState:
    day: str                          # broker-day key
    start_equity: float
    realised: float = 0.0
    trades_opened: int = 0
    session_trades: Dict[str, int] = field(default_factory=dict)
    wins: int = 0
    losses: int = 0
    min_equity: float = 0.0


@dataclass
class WeekState:
    week: str
    start_equity: float
    realised: float = 0.0
    min_equity: float = 0.0


class RiskManager:
    """Enforces every account-protection rule. All checks are pure reads —
    nothing here ever increases risk. Locks only clear on the natural
    boundary (new broker day / new week), never intra-period."""

    def __init__(self, cfg: Config, db: DatabaseManager):
        self.cfg = cfg
        self.db = db
        self.day: Optional[DayState] = None
        self.week: Optional[WeekState] = None
        self.consecutive_losses: int = int(db.get_state("consecutive_losses", 0) or 0)
        self._restore()

    # -- persistence ----------------------------------------------------------
    def _restore(self) -> None:
        d = self.db.get_state("day_state")
        if d:
            self.day = DayState(**{k: v for k, v in d.items()
                                   if k in DayState.__dataclass_fields__})
        w = self.db.get_state("week_state")
        if w:
            self.week = WeekState(**{k: v for k, v in w.items()
                                     if k in WeekState.__dataclass_fields__})

    def _persist(self) -> None:
        if self.day:
            self.db.set_state("day_state", asdict(self.day))
        if self.week:
            self.db.set_state("week_state", asdict(self.week))
        self.db.set_state("consecutive_losses", self.consecutive_losses)

    # -- period keys ------------------------------------------------------------
    def _day_key(self, t: datetime) -> str:
        shifted = t.astimezone(UTC) - timedelta(hours=self.cfg.broker_day_reset_hour_utc)
        return shifted.date().isoformat()

    @staticmethod
    def _week_key(t: datetime) -> str:
        iso = t.astimezone(UTC).isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    def roll(self, t: datetime, equity: float) -> None:
        """Advance day/week state; snapshot start-of-day equity at the
        configured broker-day reset."""
        dk = self._day_key(t)
        if self.day is None or self.day.day != dk:
            if self.day is not None:
                self.db.upsert_daily(self.day.day, self.day.start_equity,
                                     equity, self.day.realised,
                                     self.day.trades_opened, self.day.wins,
                                     self.day.losses,
                                     self._dd(self.day.start_equity,
                                              self.day.min_equity),
                                     self.lock_reason(t, equity).value)
            self.day = DayState(day=dk, start_equity=equity, min_equity=equity)
        wk = self._week_key(t)
        if self.week is None or self.week.week != wk:
            self.week = WeekState(week=wk, start_equity=equity,
                                  min_equity=equity)
        self.day.min_equity = min(self.day.min_equity or equity, equity)
        self.week.min_equity = min(self.week.min_equity or equity, equity)
        self._persist()

    @staticmethod
    def _dd(start: float, minimum: float) -> float:
        if start <= 0:
            return 0.0
        return max(0.0, (start - minimum) / start)

    # -- trade outcome bookkeeping -------------------------------------------------
    def register_open(self, t: datetime, session: SessionName) -> None:
        if self.day:
            self.day.trades_opened += 1
            key = session.value
            self.day.session_trades[key] = self.day.session_trades.get(key, 0) + 1
            self._persist()

    def register_close(self, profit: float) -> None:
        if self.day:
            self.day.realised += profit
            if profit > 0:
                self.day.wins += 1
                self.consecutive_losses = 0
            elif profit < 0:
                self.day.losses += 1
                self.consecutive_losses += 1
        if self.week:
            self.week.realised += profit
        self._persist()

    # -- lock evaluation --------------------------------------------------------------
    def lock_reason(self, t: datetime, equity: float,
                    unrealised: float = 0.0,
                    session: Optional[SessionName] = None) -> LockReason:
        cfg = self.cfg
        if self.day:
            day_pl = self.day.realised + unrealised
            base = self.day.start_equity or equity
            if base > 0:
                frac = day_pl / base
                if frac <= -cfg.max_daily_loss:
                    return LockReason.DAILY_LOSS
                if frac >= cfg.daily_profit_hard_stop:
                    return LockReason.DAILY_TARGET
            if self.day.trades_opened >= cfg.max_trades_per_day:
                return LockReason.MAX_TRADES_DAY
            if session and self.day.session_trades.get(session.value, 0) \
                    >= cfg.max_trades_per_session:
                return LockReason.MAX_TRADES_SESSION
        if self.week and self.week.start_equity > 0:
            wk_dd = (self.week.start_equity - min(self.week.min_equity,
                                                  equity + min(0.0, unrealised))) \
                / self.week.start_equity
            wk_pl = (self.week.realised + unrealised) / self.week.start_equity
            if wk_dd >= cfg.max_weekly_drawdown or wk_pl <= -cfg.max_weekly_drawdown:
                return LockReason.WEEKLY_LOSS
        if self.consecutive_losses >= cfg.max_consecutive_losses:
            return LockReason.CONSECUTIVE_LOSSES
        return LockReason.NONE

    def soft_throttle_active(self, unrealised: float = 0.0) -> bool:
        """+2% soft stop: reduce risk substantially, per config."""
        cfg = self.cfg
        if not self.day or self.day.start_equity <= 0:
            return False
        frac = (self.day.realised + unrealised) / self.day.start_equity
        return frac >= cfg.daily_profit_soft_stop

    def adjusted_risk(self, base_risk: float, unrealised: float = 0.0) -> float:
        """Apply the +2% soft-stop risk reduction. NEVER increases risk."""
        if self.soft_throttle_active(unrealised):
            return base_risk * self.cfg.soft_stop_risk_factor
        return base_risk

    def open_risk_headroom(self, equity: float,
                           open_risk_money: float) -> float:
        """Remaining combined-open-risk budget in money terms."""
        return max(0.0, equity * self.cfg.max_combined_open_risk - open_risk_money)

    def can_open(self, t: datetime, equity: float, open_positions: int,
                 open_risk_money: float, new_risk_money: float,
                 unrealised: float = 0.0,
                 session: Optional[SessionName] = None) -> Tuple[bool, str]:
        cfg = self.cfg
        lock = self.lock_reason(t, equity, unrealised, session)
        if lock != LockReason.NONE:
            return False, f"lock active: {lock.value}"
        if open_positions >= cfg.max_positions:
            return False, f"max positions ({cfg.max_positions}) reached"
        if open_risk_money + new_risk_money > equity * cfg.max_combined_open_risk + 1e-9:
            return False, (f"combined open risk "
                           f"{(open_risk_money + new_risk_money) / equity:.2%} "
                           f"> {cfg.max_combined_open_risk:.0%}")
        return True, "ok"


# ===========================================================================
# SECTION 20 — ANALYSIS CONTEXT + STRATEGY ENGINE (six entry models)
# ===========================================================================

@dataclass
class TFAnalysis:
    """Everything the engine knows about one timeframe (completed candles)."""
    timeframe: Timeframe
    candles: List[Candle]
    atr: List[float]
    structure: StructureState
    zones: List[Zone]
    fvgs: List[FairValueGap]
    order_blocks: List[OrderBlock]
    liquidity: List[LiquidityLevel]
    sweeps: List[SweepEvent]

    @property
    def last(self) -> Candle:
        return self.candles[-1]

    @property
    def atr_now(self) -> float:
        return self.atr[-1] if self.atr else 0.0


@dataclass
class AnalysisContext:
    time: datetime                    # close time of the newest base candle
    price: float                      # last close
    spread_points: float
    point: float
    tf_plan: TimeframePlan
    regime: RegimeReading
    session: SessionName
    news_blocked: bool
    news_reason: str
    news_complete: bool
    tfs: Dict[Timeframe, TFAnalysis]
    asian_range: Optional[Tuple[float, float]] = None

    def tf(self, timeframe: Timeframe) -> Optional[TFAnalysis]:
        return self.tfs.get(timeframe)

    @property
    def bias(self) -> Optional[TFAnalysis]:
        return self.tf(self.tf_plan.bias_tf)

    @property
    def structure_tf(self) -> Optional[TFAnalysis]:
        return self.tf(self.tf_plan.structure_tf)

    @property
    def decision(self) -> Optional[TFAnalysis]:
        return self.tf(self.tf_plan.decision_tf)

    @property
    def entry(self) -> Optional[TFAnalysis]:
        return self.tf(self.tf_plan.entry_tf)


@dataclass
class Candidate:
    """A raw model output before scoring/gating."""
    model: SetupModel
    direction: Direction
    zone: Optional[Zone]
    sweep: Optional[SweepEvent]
    structure_event: Optional[StructureEvent]
    fvg: Optional[FairValueGap]
    order_block: Optional[OrderBlock]
    displacement: bool
    stop_anchor: float                # raw structural invalidation price
    retest_level: Optional[float]     # limit-entry reference, None = market only
    note: str


class StrategyEngine:
    """Builds AnalysisContext and runs the six entry models. All decisions
    use completed candles only; intrabar prices are used later, purely for
    executing already-confirmed signals and for protective monitoring."""

    RECENT = 6      # candles: how recent confirmation must be
    ZONE_TOUCH_LOOKBACK = 8

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.scorer = SetupScorer(cfg)

    # ------------------------------------------------------------------ build
    def build_tf_analysis(self, tf: Timeframe,
                          candles: List[Candle],
                          session_marks: Optional[Dict[str, float]] = None,
                          external: bool = False) -> TFAnalysis:
        cfg = self.cfg
        left = cfg.external_swing_left if external else cfg.swing_left
        right = cfg.external_swing_right if external else cfg.swing_right
        analyzer = StructureAnalyzer(cfg, left, right)
        atr = atr_series(candles, cfg.atr_period)
        structure = analyzer.analyze(candles, atr)
        zones = SupplyDemandDetector(cfg, tf).detect(candles, structure)
        fvgs = FVGDetector(cfg, tf).detect(candles)
        liq_det = LiquidityDetector(cfg)
        levels = liq_det.detect_levels(candles, structure.swings, session_marks)
        sweeps = liq_det.update_states(levels, candles, atr)
        obs = OrderBlockDetector(cfg, tf).detect(candles, structure, sweeps)
        return TFAnalysis(tf, candles, atr, structure, zones, fvgs, obs,
                          levels, sweeps)

    # ------------------------------------------------------------------ helpers
    def _htf_bias(self, ctx: AnalysisContext) -> TrendState:
        bias = ctx.bias
        struct = ctx.structure_tf
        b = bias.structure.trend if bias else TrendState.UNDEFINED
        s = struct.structure.trend if struct else TrendState.UNDEFINED
        if b == s:
            return b
        if b in (TrendState.RANGING, TrendState.UNDEFINED):
            return s
        if s in (TrendState.RANGING, TrendState.UNDEFINED):
            return b
        return TrendState.RANGING     # conflict => treat as no clear bias

    def _recent_event(self, tfa: TFAnalysis, direction: Direction,
                      kinds: Tuple[StructureEventKind, ...],
                      recency: Optional[int] = None) -> Optional[StructureEvent]:
        rec = recency if recency is not None else self.RECENT
        n = len(tfa.candles)
        for ev in reversed(tfa.structure.events):
            if ev.index < n - rec:
                return None
            if ev.direction == direction and ev.kind in kinds:
                return ev
        return None

    def _recent_sweep(self, tfa: TFAnalysis, buy_side: bool,
                      recency: int = 12) -> Optional[SweepEvent]:
        n = len(tfa.candles)
        for sv in reversed(tfa.sweeps):
            if sv.index < n - recency:
                return None
            if sv.level.buy_side == buy_side and sv.valid:
                return sv
        return None

    def _zone_in_play(self, tfa: TFAnalysis, kind: ZoneKind,
                      tolerance: float) -> Optional[Zone]:
        """Zone touched by price within the last few candles."""
        recent = tfa.candles[-self.ZONE_TOUCH_LOOKBACK:]
        hi = max(c.high for c in recent)
        lo = min(c.low for c in recent)
        best: Optional[Zone] = None
        for z in SupplyDemandDetector.active_zones(tfa.zones, kind):
            touched = (z.lower - tolerance <= hi and lo <= z.upper + tolerance)
            if touched and (best is None or z.quality() > best.quality()):
                best = z
        return best

    def _confluence(self, tfa: TFAnalysis, direction: Direction,
                    around: float, tolerance: float
                    ) -> Tuple[Optional[FairValueGap], Optional[OrderBlock]]:
        fvg = None
        for g in FVGDetector.usable(tfa.fvgs, direction):
            if g.lower - tolerance <= around <= g.upper + tolerance:
                fvg = g
                break
        ob = None
        for b in OrderBlockDetector.usable(tfa.order_blocks, direction):
            if b.lower - tolerance <= around <= b.upper + tolerance:
                ob = b
                break
        return fvg, ob

    def _not_extended(self, ctx: AnalysisContext,
                      event: Optional[StructureEvent],
                      anchor: Optional[float]) -> bool:
        """Do-not-chase rule: current price must not be more than
        extension_max_atr * ATR beyond the confirmation level/anchor."""
        dec = ctx.decision
        if dec is None or dec.atr_now <= 0:
            return False
        ref = None
        if event is not None:
            ref = event.broken_level
        elif anchor is not None:
            ref = anchor
        if ref is None:
            return True
        return abs(ctx.price - ref) <= self.cfg.extension_max_atr * dec.atr_now

    # ------------------------------------------------------------------ models
    def model_trend_continuation(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 1: HTF trend + retracement into supply/demand + confirmation."""
        bias = self._htf_bias(ctx)
        if bias not in (TrendState.BULLISH, TrendState.BEARISH):
            return None
        direction = Direction.LONG if bias == TrendState.BULLISH else Direction.SHORT
        dec = ctx.decision
        struct_tf = ctx.structure_tf
        if dec is None or struct_tf is None:
            return None
        tol = 0.5 * dec.atr_now
        want_zone = ZoneKind.DEMAND if direction == Direction.LONG else ZoneKind.SUPPLY
        zone = (self._zone_in_play(struct_tf, want_zone, tol)
                or self._zone_in_play(dec, want_zone, tol))
        # broken-structure retest / OB / FVG also qualify as the "area"
        fvg, ob = self._confluence(dec, direction, ctx.price, tol)
        if zone is None and fvg is None and ob is None:
            return None
        ev = self._recent_event(dec, direction,
                                (StructureEventKind.BOS, StructureEventKind.CHOCH,
                                 StructureEventKind.MSS))
        if ev is None:
            return None
        sweep = self._recent_sweep(dec, buy_side=(direction == Direction.SHORT))
        disp = ev.displacement or is_displacement(dec.last, dec.atr_now, self.cfg)
        if not disp and sweep is None:
            return None                       # need rejection evidence
        if not self._not_extended(ctx, ev, zone.mid if zone else None):
            return None
        # structural stop anchor
        if direction == Direction.SHORT:
            anchors = [dec.last.high]
            if sweep:
                anchors.append(sweep.extreme)
            if zone:
                anchors.append(zone.upper)
            if dec.structure.protected_high:
                anchors.append(dec.structure.protected_high.price)
            stop_anchor = max(anchors)
        else:
            anchors = [dec.last.low]
            if sweep:
                anchors.append(sweep.extreme)
            if zone:
                anchors.append(zone.lower)
            if dec.structure.protected_low:
                anchors.append(dec.structure.protected_low.price)
            stop_anchor = min(anchors)
        retest = ev.broken_level
        if fvg is not None:
            retest = fvg.midpoint
        elif ob is not None:
            retest = ob.upper if direction == Direction.SHORT else ob.lower
        return Candidate(SetupModel.TREND_CONTINUATION, direction, zone, sweep,
                         ev, fvg, ob, disp, stop_anchor, retest,
                         f"{bias.value} continuation off "
                         f"{'zone ' + zone.pattern.value if zone else 'confluence'}"
                         f" with {ev.kind.value}")

    def model_liquidity_sweep_reversal(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 2: HTF zone/discount + sweep + displacement + CHoCH/MSS."""
        dec = ctx.decision
        struct_tf = ctx.structure_tf
        if dec is None or struct_tf is None:
            return None
        analyzer = StructureAnalyzer(self.cfg)
        for direction in (Direction.LONG, Direction.SHORT):
            sweep = self._recent_sweep(dec, buy_side=(direction == Direction.SHORT))
            if sweep is None:
                continue
            # meaningful HTF location: zone or premium/discount extreme
            want_zone = ZoneKind.DEMAND if direction == Direction.LONG else ZoneKind.SUPPLY
            tol = 0.5 * dec.atr_now
            zone = self._zone_in_play(struct_tf, want_zone, tol)
            pd_label, _pos = analyzer.premium_discount(struct_tf.structure, ctx.price)
            good_location = zone is not None or \
                (direction == Direction.LONG and pd_label == "DISCOUNT") or \
                (direction == Direction.SHORT and pd_label == "PREMIUM")
            if not good_location:
                continue
            # countertrend reversals demand MSS or CHoCH WITH displacement
            ev = self._recent_event(dec, direction,
                                    (StructureEventKind.CHOCH, StructureEventKind.MSS))
            if ev is None:
                continue
            bias = self._htf_bias(ctx)
            countertrend = (direction == Direction.LONG and bias == TrendState.BEARISH) \
                or (direction == Direction.SHORT and bias == TrendState.BULLISH)
            if countertrend and not (ev.kind == StructureEventKind.MSS
                                     or ev.displacement):
                continue
            # protected swing beyond the sweep must exist (structure proved)
            if direction == Direction.LONG:
                prot = dec.structure.last_confirmed_low
                if prot is None or prot.price < sweep.extreme:
                    prot_ok = prot is not None and prot.index > sweep.index
                else:
                    prot_ok = True
                if not prot_ok:
                    continue
                stop_anchor = min(sweep.extreme, dec.last.low)
            else:
                prot = dec.structure.last_confirmed_high
                if prot is None or prot.price > sweep.extreme:
                    prot_ok = prot is not None and prot.index > sweep.index
                else:
                    prot_ok = True
                if not prot_ok:
                    continue
                stop_anchor = max(sweep.extreme, dec.last.high)
            if not self._not_extended(ctx, ev, sweep.level.price):
                continue
            fvg, ob = self._confluence(dec, direction, ev.broken_level,
                                       0.5 * dec.atr_now)
            disp = ev.displacement or sweep.displaced_away
            retest = fvg.midpoint if fvg else ev.broken_level
            return Candidate(SetupModel.LIQUIDITY_SWEEP_REVERSAL, direction,
                             zone, sweep, ev, fvg, ob, disp, stop_anchor,
                             retest,
                             f"sweep of {sweep.level.kind.value} then "
                             f"{ev.kind.value} ({pd_label.lower()})")
        return None

    def model_break_retest(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 3: displacement break + close beyond + retest holding."""
        dec = ctx.decision
        if dec is None or dec.atr_now <= 0:
            return None
        cfg = self.cfg
        n = len(dec.candles)
        for ev in reversed(dec.structure.events):
            if ev.index < n - cfg.retest_max_candles:
                break
            if not ev.displacement:
                continue
            direction = ev.direction
            level = ev.broken_level
            # find a completed retest after the break: price returned to the
            # level and the latest candle rejected in the break direction
            touched = False
            for j in range(ev.index + 1, n):
                c = dec.candles[j]
                if direction == Direction.LONG and c.low <= level + 0.15 * dec.atr_now:
                    touched = True
                if direction == Direction.SHORT and c.high >= level - 0.15 * dec.atr_now:
                    touched = True
            if not touched:
                continue
            last = dec.last
            rejected = (direction == Direction.LONG and last.bullish
                        and last.close > level) or \
                       (direction == Direction.SHORT and last.bearish
                        and last.close < level)
            if not rejected:
                continue
            if not self._not_extended(ctx, ev, None):
                continue
            if direction == Direction.LONG:
                stop_anchor = min(c.low for c in dec.candles[ev.index:n])
            else:
                stop_anchor = max(c.high for c in dec.candles[ev.index:n])
            fvg, ob = self._confluence(dec, direction, level, 0.5 * dec.atr_now)
            sweep = self._recent_sweep(dec, buy_side=(direction == Direction.SHORT))
            return Candidate(SetupModel.BREAK_RETEST, direction, None, sweep,
                             ev, fvg, ob, True, stop_anchor, level,
                             f"break+retest of {level:.2f} ({ev.kind.value})")
        return None

    def model_range_extreme(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 4: sweep beyond range edge, reclaim, CHoCH toward mid."""
        if ctx.regime.regime != Regime.RANGE:
            return None
        dec = ctx.decision
        if dec is None or dec.structure.dealing_range is None:
            return None
        rng = dec.structure.dealing_range
        pos = rng.position_of(ctx.price)
        edge = self.cfg.range_edge_fraction
        if edge < pos < 1.0 - edge:
            return None                # never trade the middle of the range
        direction = Direction.LONG if pos <= edge else Direction.SHORT
        sweep = self._recent_sweep(dec, buy_side=(direction == Direction.SHORT))
        if sweep is None:
            return None
        ev = self._recent_event(dec, direction,
                                (StructureEventKind.CHOCH, StructureEventKind.MSS))
        if ev is None:
            return None
        stop_anchor = sweep.extreme
        return Candidate(SetupModel.RANGE_EXTREME, direction, None, sweep, ev,
                         None, None, ev.displacement, stop_anchor,
                         ev.broken_level,
                         f"range edge (pos {pos:.2f}) sweep+reclaim")

    def model_session_liquidity(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 5: Asian range swept by London/NY, reclaim + confirmation."""
        if ctx.session not in (SessionName.LONDON, SessionName.NEW_YORK,
                               SessionName.OVERLAP):
            return None
        if ctx.asian_range is None:
            return None
        dec = ctx.decision
        if dec is None:
            return None
        asian_low, asian_high = ctx.asian_range
        for direction, swept_side in ((Direction.LONG, False),
                                      (Direction.SHORT, True)):
            sweep = self._recent_sweep(dec, buy_side=swept_side, recency=18)
            if sweep is None:
                continue
            # the swept level must belong to the Asian range boundary area
            tol = 0.6 * dec.atr_now
            boundary = asian_low if direction == Direction.LONG else asian_high
            if abs(sweep.level.price - boundary) > tol and \
                    sweep.level.kind not in (LiquidityKind.SESSION_LOW,
                                             LiquidityKind.SESSION_HIGH):
                continue
            ev = self._recent_event(dec, direction,
                                    (StructureEventKind.CHOCH,
                                     StructureEventKind.MSS,
                                     StructureEventKind.BOS))
            if ev is None or not (ev.displacement or sweep.displaced_away):
                continue
            bias = self._htf_bias(ctx)
            against = (direction == Direction.LONG and bias == TrendState.BEARISH) \
                or (direction == Direction.SHORT and bias == TrendState.BULLISH)
            if against and ev.kind != StructureEventKind.MSS:
                continue          # session reversals against HTF need MSS
            stop_anchor = sweep.extreme
            fvg, ob = self._confluence(dec, direction, ev.broken_level,
                                       0.5 * dec.atr_now)
            return Candidate(SetupModel.SESSION_LIQUIDITY, direction, None,
                             sweep, ev, fvg, ob, True, stop_anchor,
                             ev.broken_level,
                             f"{ctx.session.value} sweep of Asian "
                             f"{'low' if direction == Direction.LONG else 'high'}")
        return None

    def model_htf_zone_reaction(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 6: D1/H4/H1 zone + LTF sweep + displacement + CHoCH."""
        entry_tfa = ctx.entry or ctx.decision
        if entry_tfa is None:
            return None
        tol_src = ctx.decision or entry_tfa
        for htf in (Timeframe.D1, Timeframe.H4, Timeframe.H1):
            tfa = ctx.tf(htf)
            if tfa is None:
                continue
            tol = 0.35 * tfa.atr_now if tfa.atr_now > 0 else 0.0
            for kind, direction in ((ZoneKind.DEMAND, Direction.LONG),
                                    (ZoneKind.SUPPLY, Direction.SHORT)):
                zone = self._zone_in_play(tfa, kind, tol)
                if zone is None:
                    continue
                # price must actually be at/near the zone now
                near = zone.lower - tol <= ctx.price <= zone.upper + tol or \
                    abs(ctx.price - zone.mid) <= 1.2 * tol_src.atr_now
                if not near:
                    continue
                sweep = self._recent_sweep(entry_tfa,
                                           buy_side=(direction == Direction.SHORT))
                ev = self._recent_event(entry_tfa, direction,
                                        (StructureEventKind.CHOCH,
                                         StructureEventKind.MSS))
                if ev is None or not (ev.displacement or (sweep and sweep.displaced_away)):
                    continue
                if direction == Direction.LONG:
                    stop_anchor = min(zone.lower,
                                      sweep.extreme if sweep else zone.lower)
                else:
                    stop_anchor = max(zone.upper,
                                      sweep.extreme if sweep else zone.upper)
                zone.htf_aligned = True
                fvg, ob = self._confluence(entry_tfa, direction,
                                           ev.broken_level,
                                           0.5 * entry_tfa.atr_now)
                return Candidate(SetupModel.HTF_ZONE_REACTION, direction,
                                 zone, sweep, ev, fvg, ob,
                                 ev.displacement, stop_anchor,
                                 fvg.midpoint if fvg else ev.broken_level,
                                 f"{htf.value} {kind.value} reaction")
        return None

    # ------------------------------------------------------------------ assembly
    MODELS_BY_REGIME: Dict[Regime, Tuple[SetupModel, ...]] = {
        Regime.STRONG_BULL: (SetupModel.TREND_CONTINUATION, SetupModel.BREAK_RETEST,
                             SetupModel.SESSION_LIQUIDITY, SetupModel.HTF_ZONE_REACTION),
        Regime.WEAK_BULL: (SetupModel.TREND_CONTINUATION, SetupModel.HTF_ZONE_REACTION,
                           SetupModel.SESSION_LIQUIDITY, SetupModel.LIQUIDITY_SWEEP_REVERSAL),
        Regime.STRONG_BEAR: (SetupModel.TREND_CONTINUATION, SetupModel.BREAK_RETEST,
                             SetupModel.SESSION_LIQUIDITY, SetupModel.HTF_ZONE_REACTION),
        Regime.WEAK_BEAR: (SetupModel.TREND_CONTINUATION, SetupModel.HTF_ZONE_REACTION,
                           SetupModel.SESSION_LIQUIDITY, SetupModel.LIQUIDITY_SWEEP_REVERSAL),
        Regime.RANGE: (SetupModel.RANGE_EXTREME, SetupModel.SESSION_LIQUIDITY,
                       SetupModel.HTF_ZONE_REACTION),
        Regime.COMPRESSION: (SetupModel.HTF_ZONE_REACTION,),
        Regime.EXPANSION: (SetupModel.BREAK_RETEST, SetupModel.TREND_CONTINUATION),
        Regime.REVERSAL_ATTEMPT: (SetupModel.LIQUIDITY_SWEEP_REVERSAL,
                                  SetupModel.HTF_ZONE_REACTION),
        Regime.NEWS_VOLATILITY: (),
        Regime.ABNORMAL_SPREAD: (),
        Regime.UNSAFE: (),
    }

    def evaluate(self, ctx: AnalysisContext,
                 cost_price_units: float,
                 reject_cb: Optional[Callable[[str, str, str], None]] = None
                 ) -> Optional[Setup]:
        """Run enabled models for the regime, score, gate, return best Setup.
        reject_cb(model, stage, reason) journals every rejected candidate."""
        cfg = self.cfg
        enabled = self.MODELS_BY_REGIME.get(ctx.regime.regime, ())
        if not enabled:
            if reject_cb:
                reject_cb("ALL", "regime", f"no models enabled in regime "
                          f"{ctx.regime.regime.value}: {ctx.regime.reason}")
            return None
        model_fns = {
            SetupModel.TREND_CONTINUATION: self.model_trend_continuation,
            SetupModel.LIQUIDITY_SWEEP_REVERSAL: self.model_liquidity_sweep_reversal,
            SetupModel.BREAK_RETEST: self.model_break_retest,
            SetupModel.RANGE_EXTREME: self.model_range_extreme,
            SetupModel.SESSION_LIQUIDITY: self.model_session_liquidity,
            SetupModel.HTF_ZONE_REACTION: self.model_htf_zone_reaction,
        }
        best: Optional[Setup] = None
        for model in enabled:
            try:
                cand = model_fns[model](ctx)
            except Exception as exc:
                log.error("model %s crashed: %s", model.value, exc)
                if reject_cb:
                    reject_cb(model.value, "exception", str(exc))
                continue
            if cand is None:
                continue
            setup = self._assemble(ctx, cand, cost_price_units, reject_cb)
            if setup is None:
                continue
            if best is None or setup.score > best.score:
                best = setup
        return best

    def _assemble(self, ctx: AnalysisContext, cand: Candidate,
                  cost: float,
                  reject_cb: Optional[Callable[[str, str, str], None]]
                  ) -> Optional[Setup]:
        cfg = self.cfg
        dec = ctx.decision
        if dec is None or dec.atr_now <= 0:
            return None
        atr = dec.atr_now
        direction = cand.direction
        price = ctx.price

        def reject(stage: str, reason: str) -> None:
            if reject_cb:
                reject_cb(cand.model.value, stage, reason)

        # ---- stop with buffer (spread + configured ATR buffer) ------------
        buffer = cfg.stop_buffer_atr * atr + ctx.spread_points * ctx.point
        stop = cand.stop_anchor + buffer if direction == Direction.SHORT \
            else cand.stop_anchor - buffer
        stop_dist = abs(price - stop)
        if stop_dist < cfg.min_stop_atr * atr:
            reject("stop", f"stop {stop_dist:.2f} inside noise "
                   f"(< {cfg.min_stop_atr} ATR = {cfg.min_stop_atr * atr:.2f})")
            return None
        if stop_dist > cfg.max_stop_atr * atr:
            reject("stop", f"stop {stop_dist:.2f} too wide "
                   f"(> {cfg.max_stop_atr} ATR = {cfg.max_stop_atr * atr:.2f})")
            return None

        # ---- entry price & mode -------------------------------------------
        entry_mode = cfg.entry_mode
        entry_price = price
        if entry_mode == EntryMode.LIMIT_ON_RETEST and cand.retest_level is not None:
            lvl = cand.retest_level
            between = (stop < lvl < price) if direction == Direction.LONG \
                else (price < lvl < stop)
            if between:
                entry_price = lvl
            elif cfg.allow_market_entries:
                entry_mode = EntryMode.MARKET_ON_CONFIRM
            else:
                reject("entry", "no valid retest level for limit entry")
                return None
        elif entry_mode == EntryMode.LIMIT_ON_RETEST:
            if cfg.allow_market_entries:
                entry_mode = EntryMode.MARKET_ON_CONFIRM
            else:
                reject("entry", "limit-only mode but no retest level")
                return None
        if cfg.alert_only:
            entry_mode = EntryMode.ALERT_ONLY
        stop_dist = abs(entry_price - stop)
        if stop_dist <= 0:
            reject("entry", "entry equals stop after retest adjustment")
            return None

        # ---- targets: logical opposing liquidity ---------------------------
        struct_tf = ctx.structure_tf or dec
        pool = list(dec.liquidity) + list(struct_tf.liquidity)
        targets = LiquidityDetector.targets_beyond(pool, entry_price,
                                                   direction, count=4)
        # opposing zone edges also act as targets
        opposing_kind = ZoneKind.SUPPLY if direction == Direction.LONG else ZoneKind.DEMAND
        for z in SupplyDemandDetector.active_zones(struct_tf.zones, opposing_kind):
            edge = z.lower if direction == Direction.LONG else z.upper
            if (direction == Direction.LONG and edge > entry_price) or \
               (direction == Direction.SHORT and edge < entry_price):
                targets.append(LiquidityLevel(new_id("liq"),
                                              LiquidityKind.RANGE_HIGH if direction == Direction.LONG
                                              else LiquidityKind.RANGE_LOW,
                                              edge, z.created_time,
                                              buy_side=(direction == Direction.LONG)))
        targets.sort(key=lambda l: l.price if direction == Direction.LONG else -l.price)
        # minimum meaningful distance for TP1: 1 ATR or min_rr, whichever larger
        min_tp1 = entry_price + direction.sign * max(atr,
                                                     cfg.min_rr * stop_dist * 0.75)
        usable = [t for t in targets
                  if (direction == Direction.LONG and t.price >= min_tp1)
                  or (direction == Direction.SHORT and t.price <= min_tp1)]
        if not usable:
            reject("target", "no opposing liquidity far enough for TP1")
            return None
        tp1 = usable[0].price
        tp2 = usable[1].price if len(usable) > 1 else \
            entry_price + direction.sign * min(cfg.preferred_rr_cap * stop_dist,
                                               2.0 * abs(tp1 - entry_price))
        runner = usable[2].price if len(usable) > 2 else None
        # never target beyond major opposing structure blindly: cap runner
        if runner is not None and abs(runner - entry_price) > cfg.preferred_rr_cap * stop_dist * 1.5:
            runner = None

        # ---- net RR check ----------------------------------------------------
        reward1 = abs(tp1 - entry_price) - cost
        risk1 = stop_dist + cost
        rr1 = reward1 / risk1 if risk1 > 0 else 0.0

        # ---- score ----------------------------------------------------------
        analyzer = StructureAnalyzer(cfg)
        pd_label, _ = analyzer.premium_discount(struct_tf.structure, entry_price)
        htf_bias = self._htf_bias(ctx)
        breakdown = self.scorer.score(
            direction=direction, htf_bias=htf_bias, zone=cand.zone,
            sweep=cand.sweep, structure_event=cand.structure_event,
            displacement=cand.displacement, fvg=cand.fvg,
            order_block=cand.order_block, pd_zone=pd_label,
            session=ctx.session, news_blocked=ctx.news_blocked,
            news_protection_complete=ctx.news_complete,
            rr_tp1=rr1, target_is_liquidity=usable[0].kind not in
            (LiquidityKind.RANGE_HIGH, LiquidityKind.RANGE_LOW))
        score = breakdown.total
        grade = SetupGrade.from_score(score)

        min_rr = cfg.min_rr_a_plus if grade == SetupGrade.A_PLUS else cfg.min_rr
        if rr1 < min_rr:
            reject("rr", f"net RR to TP1 {rr1:.2f} < required {min_rr:.2f}")
            return None

        countertrend = (direction == Direction.LONG and htf_bias == TrendState.BEARISH) \
            or (direction == Direction.SHORT and htf_bias == TrendState.BULLISH)
        threshold = cfg.countertrend_min_score if countertrend else cfg.min_score
        if ctx.session == SessionName.ASIA:
            threshold = max(threshold, cfg.asia_min_score)
        if score < threshold:
            reject("score", f"score {score:.1f} < threshold {threshold:.1f} "
                   f"({'countertrend' if countertrend else 'with-trend'}, "
                   f"session {ctx.session.value})")
            return None

        return Setup(
            setup_id=new_id("setup"), model=cand.model, direction=direction,
            created_time=ctx.time, signal_price=price,
            entry_price=entry_price, stop_price=stop, tp1=tp1, tp2=tp2,
            runner_target=runner, entry_mode=entry_mode, score=score,
            grade=grade, breakdown=breakdown, tf_plan=ctx.tf_plan,
            regime=ctx.regime.regime, session=ctx.session,
            htf_bias=htf_bias, zone=cand.zone, sweep=cand.sweep,
            structure_event=cand.structure_event, fvg=cand.fvg,
            order_block=cand.order_block, atr=atr,
            spread_points=ctx.spread_points, reason=cand.note)


# ===========================================================================
# SECTION 21 — TRADE MANAGEMENT (BE, partials, structural trailing)
# ===========================================================================

@dataclass
class ManagementAction:
    """One instruction produced by TradeManager for the executor."""
    kind: str                 # "MOVE_STOP" | "PARTIAL_CLOSE" | "CLOSE"
    trade: Trade
    price: float = 0.0        # new stop or close reference
    volume: float = 0.0       # for partial closes
    reason: ExitReason = ExitReason.MANUAL
    note: str = ""


class TradeManager:
    """Manages open trades on completed management-TF candles.

    Hard rules enforced here:
      * a stop is NEVER widened and NEVER removed;
      * break-even only with evidence (>= +1R AND, for BALANCED/
        CONSERVATIVE, a confirmed protected swing in profit direction);
      * trailing follows confirmed management-TF swings, not every M1 tick;
      * stops are kept off obvious liquidity by a small ATR offset."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _profile(self) -> Dict[str, float]:
        p = self.cfg.management_profile
        if p == ManagementProfile.CONSERVATIVE:
            return {"be_r": max(0.8, self.cfg.breakeven_r * 0.8),
                    "partial_r": max(1.2, self.cfg.partial_r * 0.8),
                    "partial_frac": min(0.6, self.cfg.partial_fraction + 0.10),
                    "trail_atr": self.cfg.trail_atr_mult * 0.8}
        if p == ManagementProfile.AGGRESSIVE:
            return {"be_r": self.cfg.breakeven_r * 1.5,
                    "partial_r": self.cfg.partial_r * 1.3,
                    "partial_frac": max(0.2, self.cfg.partial_fraction - 0.15),
                    "trail_atr": self.cfg.trail_atr_mult * 1.3}
        return {"be_r": self.cfg.breakeven_r,
                "partial_r": self.cfg.partial_r,
                "partial_frac": self.cfg.partial_fraction,
                "trail_atr": self.cfg.trail_atr_mult}

    def manage(self, trade: Trade, mgmt: TFAnalysis,
               now: datetime, spec: SymbolSpecification,
               news_exit: bool = False,
               weekend_flat: bool = False) -> List[ManagementAction]:
        cfg = self.cfg
        prof = self._profile()
        actions: List[ManagementAction] = []
        if trade.status != TradeStatus.OPEN:
            return actions
        d = trade.direction
        price = mgmt.last.close
        risk_dist = abs(trade.entry_price - trade.initial_stop)
        if risk_dist <= 0:
            return actions
        r_now = (price - trade.entry_price) * d.sign / risk_dist

        # ---- protective exits first ---------------------------------------
        if news_exit:
            actions.append(ManagementAction("CLOSE", trade, price=price,
                                            reason=ExitReason.NEWS_EXIT,
                                            note="news risk exit"))
            return actions
        if weekend_flat and not cfg.weekend_hold_allowed:
            actions.append(ManagementAction("CLOSE", trade, price=price,
                                            reason=ExitReason.WEEKEND_EXIT,
                                            note="pre-weekend flat"))
            return actions
        if trade.entry_time and (now - trade.entry_time) >= timedelta(hours=cfg.time_exit_hours):
            actions.append(ManagementAction("CLOSE", trade, price=price,
                                            reason=ExitReason.TIME_EXIT,
                                            note=f"open > {cfg.time_exit_hours}h"))
            return actions
        if trade.bars_open >= cfg.max_bars_no_progress and trade.mfe < 0.2:
            actions.append(ManagementAction("CLOSE", trade, price=price,
                                            reason=ExitReason.TIME_EXIT,
                                            note="no progress time stop"))
            return actions

        # ---- break-even ------------------------------------------------------
        if not trade.breakeven_done and r_now >= prof["be_r"]:
            structure_ok = True
            if cfg.breakeven_needs_structure:
                structure_ok = self._protected_swing_in_profit(trade, mgmt)
            if structure_ok:
                costs = 0.0
                if cfg.breakeven_costs_buffer:
                    costs = (spec.spread_points + cfg.slippage_buffer_points) \
                        * spec.point + cfg.commission_per_lot \
                        * trade.initial_volume / max(trade.initial_volume, 1e-9) \
                        / max(spec.money_per_price_unit_per_lot(), 1e-9)
                be = trade.entry_price + d.sign * costs
                if self._tightens(trade, be):
                    actions.append(ManagementAction(
                        "MOVE_STOP", trade, price=be,
                        reason=ExitReason.BREAK_EVEN,
                        note=f"BE at +{r_now:.2f}R with structure"))
                    trade.breakeven_done = True

        # ---- partial profit ---------------------------------------------------
        if not trade.partial_done and r_now >= prof["partial_r"] \
                and trade.volume > spec.volume_min:
            vol = spec.round_volume_down(trade.initial_volume * prof["partial_frac"])
            if vol >= spec.volume_min and vol < trade.volume:
                actions.append(ManagementAction(
                    "PARTIAL_CLOSE", trade, price=price, volume=vol,
                    reason=ExitReason.PARTIAL_TP,
                    note=f"partial {prof['partial_frac']:.0%} at +{r_now:.2f}R"))
                trade.partial_done = True

        # ---- structural trailing ------------------------------------------------
        trail = self._structural_trail(trade, mgmt, prof["trail_atr"])
        if trail is not None and self._tightens(trade, trail):
            actions.append(ManagementAction(
                "MOVE_STOP", trade, price=trail,
                reason=ExitReason.TRAIL_STOP,
                note="trail behind confirmed swing"))
        return actions

    def _protected_swing_in_profit(self, trade: Trade,
                                   mgmt: TFAnalysis) -> bool:
        """A confirmed swing (mgmt TF) beyond entry in the profit direction."""
        d = trade.direction
        for s in reversed(mgmt.structure.swings):
            if trade.entry_time and s.time <= trade.entry_time:
                break
            if d == Direction.LONG and s.kind == SwingKind.LOW \
                    and s.price > trade.entry_price:
                return True
            if d == Direction.SHORT and s.kind == SwingKind.HIGH \
                    and s.price < trade.entry_price:
                return True
        return False

    def _structural_trail(self, trade: Trade, mgmt: TFAnalysis,
                          trail_atr_mult: float) -> Optional[float]:
        """Stop behind the newest confirmed swing after entry, with an ATR
        offset so the stop is not resting ON obvious liquidity. An ATR
        channel acts as a safety net in runaway moves."""
        d = trade.direction
        atr = mgmt.atr_now
        candidate: Optional[float] = None
        for s in reversed(mgmt.structure.swings):
            if trade.entry_time and s.time <= trade.entry_time:
                break
            if d == Direction.LONG and s.kind == SwingKind.LOW:
                candidate = s.price - 0.35 * atr
                break
            if d == Direction.SHORT and s.kind == SwingKind.HIGH:
                candidate = s.price + 0.35 * atr
                break
        # ATR net only once in decent profit
        risk_dist = abs(trade.entry_price - trade.initial_stop)
        price = mgmt.last.close
        if risk_dist > 0 and (price - trade.entry_price) * d.sign / risk_dist >= 2.0:
            atr_net = price - d.sign * trail_atr_mult * atr
            if candidate is None or (atr_net - candidate) * d.sign > 0:
                candidate = atr_net
        return candidate

    @staticmethod
    def _tightens(trade: Trade, new_stop: float) -> bool:
        """True only if the new stop reduces risk (never widens)."""
        if trade.direction == Direction.LONG:
            return new_stop > trade.stop_price
        return new_stop < trade.stop_price


# ===========================================================================
# SECTION 22 — MT5 CONNECTOR / SYMBOL MANAGER / MARKET DATA
# ===========================================================================

class MT5Connector:
    """Guarded wrapper around the MetaTrader5 package. Every call verifies
    availability; nothing here fabricates data when MT5 is absent."""

    TF_MAP: Dict[Timeframe, int] = {}

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.connected = False
        if MT5_AVAILABLE:
            MT5Connector.TF_MAP = {
                Timeframe.M1: mt5.TIMEFRAME_M1, Timeframe.M5: mt5.TIMEFRAME_M5,
                Timeframe.M15: mt5.TIMEFRAME_M15, Timeframe.M30: mt5.TIMEFRAME_M30,
                Timeframe.H1: mt5.TIMEFRAME_H1, Timeframe.H4: mt5.TIMEFRAME_H4,
                Timeframe.D1: mt5.TIMEFRAME_D1, Timeframe.W1: mt5.TIMEFRAME_W1}

    def connect(self) -> bool:
        if not MT5_AVAILABLE:
            log.error("MetaTrader5 package not installed (Windows only) — "
                      "cannot connect. BACKTEST and --test still work.")
            return False
        kwargs: Dict[str, Any] = {}
        if self.cfg.mt5_login and self.cfg.mt5_password and self.cfg.mt5_server:
            kwargs = {"login": self.cfg.mt5_login,
                      "password": self.cfg.mt5_password,
                      "server": self.cfg.mt5_server}
        if not mt5.initialize(**kwargs):
            log.error("mt5.initialize failed: %s", mt5.last_error())
            self.connected = False
            return False
        self.connected = True
        info = mt5.account_info()
        if info is None:
            log.error("mt5.account_info returned None")
            self.connected = False
            return False
        log.info("MT5 connected: account=%s server=%s balance=%.2f "
                 "trade_allowed=%s", info.login, info.server, info.balance,
                 info.trade_allowed)
        return True

    def shutdown(self) -> None:
        if MT5_AVAILABLE and self.connected:
            mt5.shutdown()
            self.connected = False

    def account(self) -> Optional[Any]:
        if not (MT5_AVAILABLE and self.connected):
            return None
        return mt5.account_info()

    def equity(self) -> Optional[float]:
        a = self.account()
        return float(a.equity) if a else None

    def terminal_ok(self) -> bool:
        if not (MT5_AVAILABLE and self.connected):
            return False
        t = mt5.terminal_info()
        return bool(t and t.connected and t.trade_allowed)

    def verify_account(self, mode: Mode) -> Tuple[bool, str]:
        """Account/server matching + demo/live sanity."""
        a = self.account()
        if a is None:
            return False, "no account info"
        is_demo = getattr(a, "trade_mode", None) == getattr(
            mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        if mode == Mode.DEMO and not is_demo:
            return False, ("DEMO mode but the connected account is NOT a "
                           "demo account — refusing")
        if mode == Mode.LIVE:
            if is_demo:
                return False, "LIVE mode but connected account is a demo account"
            if a.login != self.cfg.live_account_number:
                return False, (f"connected account {a.login} != configured "
                               f"live_account_number {self.cfg.live_account_number}")
            if self.cfg.live_server and a.server != self.cfg.live_server:
                return False, (f"connected server {a.server} != configured "
                               f"live_server {self.cfg.live_server}")
        return True, "ok"


class SymbolManager:
    """Detects the broker's gold symbol and reads its REAL specification.
    Never assumes a universal pip definition."""

    def __init__(self, cfg: Config, connector: MT5Connector):
        self.cfg = cfg
        self.connector = connector
        self.spec: Optional[SymbolSpecification] = None

    def detect(self) -> Optional[SymbolSpecification]:
        if not (MT5_AVAILABLE and self.connector.connected):
            return None
        names: List[str] = []
        if self.cfg.symbol_override:
            names = [self.cfg.symbol_override]
        else:
            names = list(self.cfg.symbol_preference)
            # scan for anything gold-like as a fallback
            allsyms = mt5.symbols_get() or ()
            for s in allsyms:
                up = s.name.upper()
                if ("XAU" in up or "GOLD" in up) and s.name not in names:
                    names.append(s.name)
        for name in names:
            info = mt5.symbol_info(name)
            if info is None:
                continue
            if not info.visible and not mt5.symbol_select(name, True):
                continue
            info = mt5.symbol_info(name)
            if info is None:
                continue
            fillings = []
            fm = getattr(info, "filling_mode", 0)
            if fm & 1:
                fillings.append("FOK")
            if fm & 2:
                fillings.append("IOC")
            if not fillings:
                fillings = ["RETURN"]
            acct = self.connector.account()
            self.spec = SymbolSpecification(
                name=info.name, digits=info.digits, point=info.point,
                tick_size=info.trade_tick_size or info.point,
                tick_value=info.trade_tick_value,
                contract_size=info.trade_contract_size,
                volume_min=info.volume_min, volume_max=info.volume_max,
                volume_step=info.volume_step,
                stops_level_points=float(info.trade_stops_level),
                freeze_level_points=float(info.trade_freeze_level),
                spread_points=float(info.spread),
                trade_allowed=(info.trade_mode != 0),
                currency_profit=info.currency_profit,
                currency_account=acct.currency if acct else "USD",
                filling_modes=tuple(fillings))
            log.info("gold symbol detected: %s digits=%d point=%s "
                     "tick_value=%.4f contract=%.0f vol=[%s..%s step %s] "
                     "stops_level=%s", info.name, info.digits, info.point,
                     info.trade_tick_value, info.trade_contract_size,
                     info.volume_min, info.volume_max, info.volume_step,
                     info.trade_stops_level)
            return self.spec
        log.error("no gold symbol found among candidates: %s", names)
        return None

    def refresh_spread(self) -> float:
        """Current spread in points from a live tick."""
        if not (MT5_AVAILABLE and self.connector.connected and self.spec):
            return 0.0
        tick = mt5.symbol_info_tick(self.spec.name)
        if tick is None or self.spec.point <= 0:
            return 0.0
        return (tick.ask - tick.bid) / self.spec.point


class MarketDataManager:
    """Fetches completed candles from MT5 and converts them to Candle
    objects. copy_rates_from_pos(..., 1, n) skips the forming bar, so only
    COMPLETED candles are ever returned. Detects stale data."""

    def __init__(self, cfg: Config, connector: MT5Connector,
                 symbols: SymbolManager):
        self.cfg = cfg
        self.connector = connector
        self.symbols = symbols
        self.last_update: Optional[datetime] = None

    def candles(self, tf: Timeframe, count: int) -> List[Candle]:
        if not (MT5_AVAILABLE and self.connector.connected and self.symbols.spec):
            return []
        rates = mt5.copy_rates_from_pos(self.symbols.spec.name,
                                        MT5Connector.TF_MAP[tf], 1, count)
        if rates is None:
            log.warning("copy_rates_from_pos returned None for %s: %s",
                        tf.value, mt5.last_error())
            return []
        out: List[Candle] = []
        for r in rates:
            out.append(Candle(
                time=datetime.fromtimestamp(int(r["time"]), tz=UTC),
                open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]),
                volume=float(r["tick_volume"]),
                spread=float(r["spread"])))
        if out:
            self.last_update = utcnow()
        return out

    def tick_price(self) -> Optional[Tuple[float, float]]:
        """(bid, ask) or None."""
        if not (MT5_AVAILABLE and self.connector.connected and self.symbols.spec):
            return None
        t = mt5.symbol_info_tick(self.symbols.spec.name)
        if t is None:
            return None
        return float(t.bid), float(t.ask)

    def data_fresh(self, base_tf: Timeframe) -> bool:
        """The newest completed base candle must be recent enough."""
        cs = self.candles(base_tf, 2)
        if not cs:
            return False
        age = (utcnow() - cs[-1].time).total_seconds()
        # completed candle age can be up to 2x tf + tolerance on weekends
        return age < base_tf.seconds * 2 + self.cfg.stale_data_seconds


# ===========================================================================
# SECTION 23 — EXECUTION (paper simulator + MT5 order path)
# ===========================================================================

class ExecutionError(Exception):
    pass


class ExecutionManager:
    """Order lifecycle for every mode.

    PAPER/BACKTEST : fills simulated with spread + slippage buffer.
    DEMO/LIVE      : real MT5 order_send behind the full pre-flight
                     checklist; limited retries; reconciliation before any
                     retry when the previous status is uncertain."""

    def __init__(self, cfg: Config, db: DatabaseManager,
                 connector: Optional[MT5Connector] = None,
                 symbols: Optional[SymbolManager] = None):
        self.cfg = cfg
        self.db = db
        self.connector = connector
        self.symbols = symbols
        self.open_trades: List[Trade] = []
        self.pending_trades: List[Trade] = []
        self.closed_trades: List[Trade] = []
        self._recent_setup_keys: Dict[str, datetime] = {}

    # ------------------------------------------------------------ duplicate guard
    def _setup_key(self, setup: Setup) -> str:
        return f"{setup.model.value}|{setup.direction.value}|{round(setup.entry_price, 1)}"

    def duplicate(self, setup: Setup) -> bool:
        key = self._setup_key(setup)
        t = self._recent_setup_keys.get(key)
        if t and (setup.created_time - t) < timedelta(hours=2):
            return True
        for tr in self.open_trades + self.pending_trades:
            if tr.setup.direction == setup.direction and \
                    abs(tr.setup.entry_price - setup.entry_price) < setup.atr * 0.5:
                return True
        return False

    def conflicting(self, setup: Setup) -> bool:
        return any(tr.direction != setup.direction
                   for tr in self.open_trades + self.pending_trades)

    def open_risk_money(self) -> float:
        total = 0.0
        for tr in self.open_trades:
            # remaining risk = distance from current stop to entry * volume value
            total += max(0.0, tr.risk_money * (tr.volume / max(tr.initial_volume, 1e-9))
                         if self._still_risky(tr) else 0.0)
        for tr in self.pending_trades:
            total += tr.risk_money
        return total

    @staticmethod
    def _still_risky(tr: Trade) -> bool:
        if tr.direction == Direction.LONG:
            return tr.stop_price < tr.entry_price
        return tr.stop_price > tr.entry_price

    # ------------------------------------------------------------ paper path
    def paper_submit(self, setup: Setup, sizing: SizingResult,
                     spec: SymbolSpecification, now: datetime) -> Trade:
        trade = Trade(
            trade_id=new_id("trade"), setup=setup,
            status=TradeStatus.PENDING if setup.entry_mode == EntryMode.LIMIT_ON_RETEST
            else TradeStatus.OPEN,
            volume=sizing.volume, initial_volume=sizing.volume,
            risk_fraction=sizing.risk_fraction_actual,
            risk_money=sizing.risk_money,
            stop_price=setup.stop_price, initial_stop=setup.stop_price,
            tp1=setup.tp1, tp2=setup.tp2)
        slip = self.cfg.slippage_buffer_points * spec.point
        spread = max(setup.spread_points, spec.spread_points) * spec.point
        if trade.status == TradeStatus.OPEN:
            # market fill: pay half-spread + slippage against us
            trade.entry_price = setup.entry_price + setup.direction.sign * (spread / 2 + slip)
            trade.entry_time = now
            trade.commission = self.cfg.commission_per_lot * sizing.volume
            self.open_trades.append(trade)
        else:
            trade.pending_expiry = now + timedelta(
                seconds=self.cfg.limit_expiry_candles
                * Timeframe(self.cfg.backtest_base_timeframe).seconds)
            self.pending_trades.append(trade)
        self._recent_setup_keys[self._setup_key(setup)] = now
        return trade

    def paper_check_pending(self, candle: Candle, spec: SymbolSpecification,
                            now: datetime) -> List[Trade]:
        """Fill or expire pending limit orders using the completed candle.
        A limit fills if the candle traded through the limit price."""
        filled: List[Trade] = []
        remaining: List[Trade] = []
        slip = self.cfg.slippage_buffer_points * spec.point
        for tr in self.pending_trades:
            s = tr.setup
            hit = candle.low <= s.entry_price if s.direction == Direction.LONG \
                else candle.high >= s.entry_price
            # invalidated before fill: candle already beyond stop
            invalid = candle.low <= tr.stop_price if s.direction == Direction.LONG \
                else candle.high >= tr.stop_price
            if hit and not invalid:
                tr.status = TradeStatus.OPEN
                tr.entry_price = s.entry_price + s.direction.sign * slip
                tr.entry_time = now
                tr.commission = self.cfg.commission_per_lot * tr.initial_volume
                self.open_trades.append(tr)
                filled.append(tr)
            elif invalid or (tr.pending_expiry and now >= tr.pending_expiry):
                tr.status = TradeStatus.CANCELLED
                tr.exit_reason = ExitReason.MANUAL
                tr.exit_time = now
                self.closed_trades.append(tr)
            else:
                remaining.append(tr)
        self.pending_trades = remaining
        return filled

    def paper_update_open(self, candle: Candle, spec: SymbolSpecification,
                          now: datetime) -> List[Trade]:
        """Intrabar protective monitoring on a completed candle.

        ASSUMPTION (documented): when SL and TP both lie inside the candle
        the STOP is assumed to fill first (conservative, no tick data).
        Gap opens fill at the open price."""
        closed: List[Trade] = []
        money = spec.money_per_price_unit_per_lot()
        slip = self.cfg.slippage_buffer_points * spec.point
        spread = max(candle.spread, spec.spread_points) * spec.point
        for tr in list(self.open_trades):
            d = tr.direction
            risk_dist = abs(tr.entry_price - tr.initial_stop)
            # excursion tracking (uses candle extremes; monitoring only)
            if risk_dist > 0:
                fav = (candle.high - tr.entry_price) if d == Direction.LONG \
                    else (tr.entry_price - candle.low)
                adv = (tr.entry_price - candle.low) if d == Direction.LONG \
                    else (candle.high - tr.entry_price)
                tr.mfe = max(tr.mfe, fav / risk_dist)
                tr.mae = max(tr.mae, adv / risk_dist)
            tr.bars_open += 1
            # stop check (bid/ask approximated with spread on the far side)
            stop_hit = (candle.low - spread <= tr.stop_price) if d == Direction.LONG \
                else (candle.high + spread >= tr.stop_price)
            tp = tr.tp2 if tr.partial_done else tr.tp1
            tp_hit = (candle.high >= tp) if d == Direction.LONG \
                else (candle.low <= tp)
            if stop_hit:
                fill = tr.stop_price
                if d == Direction.LONG and candle.open < tr.stop_price:
                    fill = candle.open        # gap through the stop
                if d == Direction.SHORT and candle.open > tr.stop_price:
                    fill = candle.open
                fill -= d.sign * slip
                self._paper_close(tr, fill, now,
                                  ExitReason.BREAK_EVEN if tr.breakeven_done
                                  and abs(fill - tr.entry_price) < risk_dist * 0.3
                                  else (ExitReason.TRAIL_STOP
                                        if tr.stop_price != tr.initial_stop
                                        else ExitReason.STOP_LOSS),
                                  money)
                closed.append(tr)
            elif tp_hit:
                fill = tp
                if d == Direction.LONG and candle.open > tp:
                    fill = candle.open
                if d == Direction.SHORT and candle.open < tp:
                    fill = candle.open
                if not tr.partial_done and tr.tp2 != tr.tp1:
                    # scale out half at TP1, run rest to TP2
                    vol = spec.round_volume_down(tr.volume * 0.5)
                    if vol >= spec.volume_min and vol < tr.volume:
                        profit = (fill - tr.entry_price) * d.sign * money * vol
                        tr.partials.append(PartialFill(now, fill, vol,
                                                       ExitReason.PARTIAL_TP,
                                                       profit))
                        tr.profit += profit
                        tr.volume = spec.round_volume_down(tr.volume - vol)
                        tr.partial_done = True
                        # protect remainder at entry +- costs
                        be = tr.entry_price + d.sign * (spread + slip)
                        if TradeManager._tightens(tr, be):
                            tr.stop_price = be
                        continue
                self._paper_close(tr, fill, now, ExitReason.TAKE_PROFIT, money)
                closed.append(tr)
        return closed

    def _paper_close(self, tr: Trade, fill: float, now: datetime,
                     reason: ExitReason, money_per_unit: float) -> None:
        d = tr.direction
        pnl = (fill - tr.entry_price) * d.sign * money_per_unit * tr.volume
        tr.profit += pnl - self.cfg.commission_per_lot * tr.volume
        tr.commission += self.cfg.commission_per_lot * tr.volume
        tr.exit_price = fill
        tr.exit_time = now
        tr.exit_reason = reason
        tr.status = TradeStatus.CLOSED
        tr.volume = 0.0
        if tr in self.open_trades:
            self.open_trades.remove(tr)
        self.closed_trades.append(tr)

    def paper_apply_action(self, action: ManagementAction,
                           spec: SymbolSpecification, now: datetime) -> None:
        tr = action.trade
        if action.kind == "MOVE_STOP":
            if TradeManager._tightens(tr, action.price):
                tr.stop_price = action.price
        elif action.kind == "PARTIAL_CLOSE" and tr.status == TradeStatus.OPEN:
            money = spec.money_per_price_unit_per_lot()
            vol = min(action.volume, tr.volume)
            profit = (action.price - tr.entry_price) * tr.direction.sign * money * vol
            tr.partials.append(PartialFill(now, action.price, vol,
                                           action.reason, profit))
            tr.profit += profit - self.cfg.commission_per_lot * vol
            tr.commission += self.cfg.commission_per_lot * vol
            tr.volume = spec.round_volume_down(tr.volume - vol)
            tr.partial_done = True
        elif action.kind == "CLOSE" and tr.status == TradeStatus.OPEN:
            money = spec.money_per_price_unit_per_lot()
            self._paper_close(tr, action.price, now, action.reason, money)

    def paper_unrealised(self, price: float, spec: SymbolSpecification) -> float:
        money = spec.money_per_price_unit_per_lot()
        total = 0.0
        for tr in self.open_trades:
            total += (price - tr.entry_price) * tr.direction.sign * money * tr.volume
        return total

    # ------------------------------------------------------------ MT5 path
    def preflight(self, setup: Setup, sizing: SizingResult, mode: Mode,
                  news_blocked: bool, lock: LockReason,
                  signal_age_s: float, price_now: float) -> Tuple[bool, str]:
        """Full pre-order checklist (spec section 21)."""
        cfg = self.cfg
        checks: List[Tuple[bool, str]] = []
        if mode in (Mode.DEMO, Mode.LIVE):
            conn = self.connector
            sym = self.symbols
            checks.append((bool(conn and conn.terminal_ok()),
                           "terminal connected & trading allowed"))
            if conn:
                ok, why = conn.verify_account(mode)
                checks.append((ok, f"account verification: {why}"))
            checks.append((bool(sym and sym.spec and sym.spec.trade_allowed),
                           "symbol selected & tradeable"))
            acct = conn.account() if conn else None
            if acct is not None and sizing.risk_money > 0:
                checks.append((acct.margin_free > sizing.risk_money * 3,
                               "sufficient free margin"))
            if sym and sym.spec:
                spread = sym.refresh_spread()
                checks.append((spread <= cfg.max_spread_points,
                               f"spread {spread:.0f} <= {cfg.max_spread_points:.0f}"))
                min_stop = sym.spec.stops_level_points * sym.spec.point
                checks.append((setup.stop_distance >= min_stop,
                               "stop respects broker stops level"))
        checks.append((sizing.volume > 0 and not sizing.rejected,
                       f"volume valid ({sizing.reason or 'ok'})"))
        checks.append((signal_age_s <= cfg.signal_max_age_seconds,
                       f"signal fresh ({signal_age_s:.0f}s)"))
        drift = abs(price_now - setup.signal_price)
        checks.append((setup.atr <= 0 or drift <= cfg.max_price_drift_atr * setup.atr,
                       f"price drift {drift:.2f} within limit"))
        checks.append((not news_blocked, "no news blackout"))
        checks.append((lock == LockReason.NONE, f"no risk lock ({lock.value})"))
        checks.append((not self.duplicate(setup), "no duplicate setup/order"))
        checks.append((not self.conflicting(setup), "no conflicting position"))
        for ok, label in checks:
            if not ok:
                return False, label
        return True, "all preflight checks passed"

    def mt5_submit(self, setup: Setup, sizing: SizingResult,
                   mode: Mode) -> Optional[Trade]:
        """Send a real order (DEMO/LIVE). Limited retries; on uncertain
        status reconcile with MT5 before any retry."""
        if not (MT5_AVAILABLE and self.connector and self.connector.connected
                and self.symbols and self.symbols.spec):
            raise ExecutionError("MT5 not connected")
        spec = self.symbols.spec
        tick = mt5.symbol_info_tick(spec.name)
        if tick is None:
            raise ExecutionError("no tick data")
        is_limit = setup.entry_mode == EntryMode.LIMIT_ON_RETEST
        if setup.direction == Direction.LONG:
            order_type = mt5.ORDER_TYPE_BUY_LIMIT if is_limit else mt5.ORDER_TYPE_BUY
            price = setup.entry_price if is_limit else tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL_LIMIT if is_limit else mt5.ORDER_TYPE_SELL
            price = setup.entry_price if is_limit else tick.bid
        digits = spec.digits
        filling = mt5.ORDER_FILLING_IOC if "IOC" in spec.filling_modes \
            else (mt5.ORDER_FILLING_FOK if "FOK" in spec.filling_modes
                  else mt5.ORDER_FILLING_RETURN)
        request = {
            "action": mt5.TRADE_ACTION_PENDING if is_limit else mt5.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": sizing.volume,
            "type": order_type,
            "price": round(price, digits),
            "sl": round(setup.stop_price, digits),
            "tp": round(setup.tp1, digits),
            "deviation": int(self.cfg.max_slippage_points),
            "magic": self.cfg.magic_number,
            "comment": f"{BOT_NAME[:12]}:{setup.setup_id[-8:]}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        attempts = 0
        while attempts <= self.cfg.max_order_retries:
            attempts += 1
            result = mt5.order_send(request)
            if result is None:
                # UNCERTAIN status: reconcile before considering a retry
                log.error("order_send returned None: %s — reconciling",
                          mt5.last_error())
                self.reconcile()
                if self._order_exists(setup):
                    log.warning("order was actually placed; not retrying")
                    break
                if attempts > self.cfg.max_order_retries:
                    return None
                time_mod.sleep(1.0)
                continue
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                trade = Trade(
                    trade_id=new_id("trade"), setup=setup,
                    status=TradeStatus.PENDING if is_limit else TradeStatus.OPEN,
                    volume=sizing.volume, initial_volume=sizing.volume,
                    risk_fraction=sizing.risk_fraction_actual,
                    risk_money=sizing.risk_money,
                    entry_price=result.price or price,
                    entry_time=None if is_limit else utcnow(),
                    stop_price=setup.stop_price, initial_stop=setup.stop_price,
                    tp1=setup.tp1, tp2=setup.tp2,
                    mt5_ticket=result.order)
                (self.pending_trades if is_limit else self.open_trades).append(trade)
                self._recent_setup_keys[self._setup_key(setup)] = utcnow()
                log.info("order accepted ticket=%s vol=%.2f price=%.2f",
                         result.order, sizing.volume, request["price"])
                return trade
            if result.retcode in (mt5.TRADE_RETCODE_REQUOTE,
                                  mt5.TRADE_RETCODE_PRICE_CHANGED,
                                  mt5.TRADE_RETCODE_PRICE_OFF):
                log.warning("retryable retcode %s (attempt %d)",
                            result.retcode, attempts)
                tick = mt5.symbol_info_tick(spec.name)
                if tick and not is_limit:
                    request["price"] = round(
                        tick.ask if setup.direction == Direction.LONG else tick.bid,
                        digits)
                continue
            log.error("order rejected retcode=%s comment=%s",
                      result.retcode, getattr(result, "comment", ""))
            return None
        return None

    def _order_exists(self, setup: Setup) -> bool:
        if not MT5_AVAILABLE:
            return False
        for coll in (mt5.positions_get(symbol=self.symbols.spec.name) or (),
                     mt5.orders_get(symbol=self.symbols.spec.name) or ()):
            for item in coll:
                if getattr(item, "magic", 0) == self.cfg.magic_number and \
                        setup.setup_id[-8:] in getattr(item, "comment", ""):
                    return True
        return False

    def reconcile(self) -> None:
        """Match internal state to MT5 positions/orders after restart or an
        uncertain order status. Positions found at the broker but unknown
        internally are adopted; internal trades no longer at the broker are
        marked closed."""
        if not (MT5_AVAILABLE and self.connector and self.connector.connected
                and self.symbols and self.symbols.spec):
            return
        positions = mt5.positions_get(symbol=self.symbols.spec.name) or ()
        broker_tickets = {p.ticket for p in positions
                          if p.magic == self.cfg.magic_number}
        for tr in list(self.open_trades):
            if tr.mt5_ticket and tr.mt5_ticket not in broker_tickets:
                log.warning("trade %s ticket %s vanished at broker — marking "
                            "closed for reconciliation", tr.trade_id,
                            tr.mt5_ticket)
                tr.status = TradeStatus.CLOSED
                tr.exit_reason = ExitReason.MANUAL
                tr.exit_time = utcnow()
                self.open_trades.remove(tr)
                self.closed_trades.append(tr)
        known = {tr.mt5_ticket for tr in self.open_trades}
        for p in positions:
            if p.magic != self.cfg.magic_number or p.ticket in known:
                continue
            log.warning("adopting unknown broker position ticket=%s", p.ticket)
            direction = Direction.LONG if p.type == mt5.POSITION_TYPE_BUY \
                else Direction.SHORT
            dummy_plan = TimeframePlan(Timeframe.H1, Timeframe.M15,
                                       Timeframe.M15, Timeframe.M5,
                                       Timeframe.M15, "reconciled")
            setup = Setup(
                setup_id=new_id("setup"), model=SetupModel.TREND_CONTINUATION,
                direction=direction, created_time=utcnow(),
                signal_price=p.price_open, entry_price=p.price_open,
                stop_price=p.sl or 0.0, tp1=p.tp or 0.0, tp2=p.tp or 0.0,
                runner_target=None, entry_mode=EntryMode.MARKET_ON_CONFIRM,
                score=0.0, grade=SetupGrade.NO_TRADE,
                breakdown=ScoreBreakdown(), tf_plan=dummy_plan,
                regime=Regime.UNSAFE, session=SessionName.OFF_HOURS,
                htf_bias=TrendState.UNDEFINED, reason="adopted on reconcile")
            self.open_trades.append(Trade(
                trade_id=new_id("trade"), setup=setup, status=TradeStatus.OPEN,
                volume=p.volume, initial_volume=p.volume, risk_fraction=0.0,
                risk_money=0.0, entry_price=p.price_open,
                entry_time=datetime.fromtimestamp(p.time, tz=UTC),
                stop_price=p.sl or 0.0, initial_stop=p.sl or 0.0,
                tp1=p.tp or 0.0, tp2=p.tp or 0.0, mt5_ticket=p.ticket))

    def mt5_modify_stop(self, trade: Trade, new_stop: float) -> bool:
        if not (MT5_AVAILABLE and trade.mt5_ticket):
            return False
        if not TradeManager._tightens(trade, new_stop):
            return False
        spec = self.symbols.spec
        request = {"action": mt5.TRADE_ACTION_SLTP, "symbol": spec.name,
                   "position": trade.mt5_ticket,
                   "sl": round(new_stop, spec.digits),
                   "tp": round(trade.tp2 if trade.partial_done else trade.tp1,
                               spec.digits)}
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            trade.stop_price = new_stop
            return True
        log.error("modify stop failed: %s",
                  getattr(result, "retcode", "None"))
        return False

    def mt5_close(self, trade: Trade, volume: Optional[float] = None,
                  reason: ExitReason = ExitReason.MANUAL) -> bool:
        if not (MT5_AVAILABLE and trade.mt5_ticket):
            return False
        spec = self.symbols.spec
        tick = mt5.symbol_info_tick(spec.name)
        if tick is None:
            return False
        vol = volume or trade.volume
        price = tick.bid if trade.direction == Direction.LONG else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": spec.name,
            "position": trade.mt5_ticket, "volume": vol,
            "type": mt5.ORDER_TYPE_SELL if trade.direction == Direction.LONG
            else mt5.ORDER_TYPE_BUY,
            "price": price, "deviation": int(self.cfg.max_slippage_points),
            "magic": self.cfg.magic_number,
            "comment": f"close:{reason.value[:16]}",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        ok = bool(result and result.retcode == mt5.TRADE_RETCODE_DONE)
        if not ok:
            log.error("close failed for %s: %s", trade.trade_id,
                      getattr(result, "retcode", None))
            self.reconcile()
        return ok

    def emergency_close_all(self) -> None:
        log.critical("EMERGENCY CLOSE: closing all bot positions")
        for tr in list(self.open_trades):
            if tr.mt5_ticket:
                self.mt5_close(tr, reason=ExitReason.EMERGENCY)
            else:
                tr.status = TradeStatus.CLOSED
                tr.exit_reason = ExitReason.EMERGENCY
                tr.exit_time = utcnow()
                self.open_trades.remove(tr)
                self.closed_trades.append(tr)


# ===========================================================================
# SECTION 24 — TELEGRAM ALERTS
# ===========================================================================

class TelegramNotifier:
    """Optional alerts. Sends nothing when unconfigured; never includes
    credentials in messages or logs."""

    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.enabled = bool(cfg.telegram_token and cfg.telegram_chat_id
                            and REQUESTS_AVAILABLE)
        if cfg.telegram_token and not REQUESTS_AVAILABLE:
            log.warning("telegram configured but 'requests' missing")

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            resp = _requests.post(
                self.API.format(token=self.cfg.telegram_token),
                json={"chat_id": self.cfg.telegram_chat_id, "text": text,
                      "disable_web_page_preview": True},
                timeout=10)
            return resp.status_code == 200
        except Exception as exc:
            log.warning("telegram send failed: %s", exc)
            return False

    def structured(self, immediate: str, key_levels: str, plan: str,
                   verdict: Verdict, invalidation: str,
                   next_trigger: str) -> bool:
        msg = (f"A. IMMEDIATE READ\n{immediate}\n\n"
               f"B. KEY LEVELS\n{key_levels}\n\n"
               f"C. TRADE PLAN\n{plan}\n\n"
               f"D. ACTION VERDICT\n{verdict.value}\n\n"
               f"E. INVALIDATION\n{invalidation}\n\n"
               f"F. NEXT REASSESSMENT\n{next_trigger}")
        return self.send(msg)

    def event(self, title: str, detail: str = "") -> bool:
        return self.send(f"[{BOT_NAME}] {title}" + (f"\n{detail}" if detail else ""))


# ===========================================================================
# SECTION 25 — HEALTH MONITOR
# ===========================================================================

class HealthMonitor:
    """Heartbeat, kill switch, stale data and connection watchdog."""

    def __init__(self, cfg: Config, db: DatabaseManager):
        self.cfg = cfg
        self.db = db
        self.last_heartbeat: Optional[datetime] = None

    def kill_switch_active(self) -> bool:
        return Path(self.cfg.kill_switch_file).exists()

    def heartbeat(self, mode: Mode, equity: float, open_trades: int,
                  lock: str, note: str = "") -> None:
        now = utcnow()
        if self.last_heartbeat and \
                (now - self.last_heartbeat).total_seconds() < self.cfg.heartbeat_seconds:
            return
        self.last_heartbeat = now
        self.db.heartbeat(mode, equity, open_trades, lock, note)

    def check(self, connector: Optional[MT5Connector],
              data: Optional[MarketDataManager],
              base_tf: Timeframe) -> LockReason:
        if self.kill_switch_active():
            return LockReason.KILL_SWITCH
        if connector is not None and not connector.terminal_ok():
            return LockReason.DISCONNECTED
        if data is not None and not data.data_fresh(base_tf):
            return LockReason.STALE_DATA
        return LockReason.NONE


# ===========================================================================
# SECTION 26 — PERFORMANCE ANALYTICS
# ===========================================================================

class PerformanceAnalyzer:
    """Computes the research metric set from closed trades + equity curve."""

    @staticmethod
    def metrics(trades: Sequence[Trade], equity_curve: Sequence[Tuple[datetime, float]],
                initial_equity: float) -> Dict[str, Any]:
        closed = [t for t in trades if t.status == TradeStatus.CLOSED
                  and t.entry_time is not None]
        m: Dict[str, Any] = {"trades": len(closed)}
        if not closed:
            m.update({"net_return": 0.0, "profit_factor": 0.0,
                      "expectancy_r": 0.0, "max_drawdown": 0.0,
                      "win_rate": 0.0, "note": "no closed trades"})
            return m
        profits = [t.profit for t in closed]
        rs = [t.r_multiple() for t in closed if t.risk_money > 0]
        wins = [p for p in profits if p > 0]
        losses = [p for p in profits if p < 0]
        gross_profit = sum(wins)
        gross_loss = -sum(losses)
        final_equity = equity_curve[-1][1] if equity_curve else \
            initial_equity + sum(profits)
        m["gross_profit"] = round(gross_profit, 2)
        m["gross_loss"] = round(gross_loss, 2)
        m["net_profit"] = round(sum(profits), 2)
        m["net_return"] = round(sum(profits) / initial_equity, 4)
        m["win_rate"] = round(len(wins) / len(closed), 4)
        m["loss_rate"] = round(len(losses) / len(closed), 4)
        m["profit_factor"] = round(gross_profit / gross_loss, 3) \
            if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        m["expectancy_money"] = round(statistics.mean(profits), 2)
        m["expectancy_r"] = round(statistics.mean(rs), 3) if rs else 0.0
        m["avg_r"] = m["expectancy_r"]
        m["median_r"] = round(statistics.median(rs), 3) if rs else 0.0

        # drawdowns from the equity curve
        peak = initial_equity
        max_dd = 0.0
        daily_equity: Dict[str, float] = {}
        weekly_equity: Dict[str, float] = {}
        for t, eq in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)
            daily_equity[t.date().isoformat()] = eq
            iso = t.isocalendar()
            weekly_equity[f"{iso.year}-W{iso.week:02d}"] = eq
        m["max_drawdown"] = round(max_dd, 4)

        def _period_dd(series: Dict[str, float]) -> float:
            vals = list(series.values())
            worst = 0.0
            for i in range(1, len(vals)):
                if vals[i - 1] > 0:
                    worst = min(worst, (vals[i] - vals[i - 1]) / vals[i - 1])
            return -worst
        m["worst_daily_drawdown"] = round(_period_dd(daily_equity), 4)
        m["worst_weekly_drawdown"] = round(_period_dd(weekly_equity), 4)

        # sharpe/sortino-like from daily equity changes
        dvals = list(daily_equity.values())
        drets = [(dvals[i] - dvals[i - 1]) / dvals[i - 1]
                 for i in range(1, len(dvals)) if dvals[i - 1] > 0]
        if len(drets) >= 5 and statistics.pstdev(drets) > 0:
            m["sharpe_like"] = round(statistics.mean(drets)
                                     / statistics.pstdev(drets) * math.sqrt(252), 3)
            downs = [r for r in drets if r < 0]
            dstd = statistics.pstdev(downs) if len(downs) >= 2 else 0.0
            m["sortino_like"] = round(statistics.mean(drets) / dstd * math.sqrt(252), 3) \
                if dstd > 0 else 0.0
        else:
            m["sharpe_like"] = 0.0
            m["sortino_like"] = 0.0
        m["recovery_factor"] = round(m["net_profit"] / (max_dd * initial_equity), 3) \
            if max_dd > 0 else 0.0

        # streaks
        streak = w_streak = l_streak = 0
        last_sign = 0
        for p in profits:
            sign = 1 if p > 0 else (-1 if p < 0 else 0)
            streak = streak + 1 if sign == last_sign and sign != 0 else (1 if sign != 0 else 0)
            last_sign = sign if sign != 0 else last_sign
            if sign > 0:
                w_streak = max(w_streak, streak)
            elif sign < 0:
                l_streak = max(l_streak, streak)
        m["longest_win_streak"] = w_streak
        m["longest_loss_streak"] = l_streak

        # exposure: fraction of the tested span with an open position
        if equity_curve and closed:
            span = (equity_curve[-1][0] - equity_curve[0][0]).total_seconds()
            open_secs = sum(((t.exit_time or t.entry_time) - t.entry_time).total_seconds()
                            for t in closed)
            m["exposure"] = round(min(1.0, open_secs / span), 4) if span > 0 else 0.0
        else:
            m["exposure"] = 0.0

        # breakdowns
        def _bucketize(key_fn: Callable[[Trade], str]) -> Dict[str, Dict[str, float]]:
            buckets: Dict[str, List[Trade]] = {}
            for t in closed:
                buckets.setdefault(key_fn(t), []).append(t)
            out = {}
            for k, ts in sorted(buckets.items()):
                rr = [t.r_multiple() for t in ts if t.risk_money > 0]
                out[k] = {"trades": len(ts),
                          "net": round(sum(t.profit for t in ts), 2),
                          "win_rate": round(sum(1 for t in ts if t.profit > 0)
                                            / len(ts), 3),
                          "avg_r": round(statistics.mean(rr), 3) if rr else 0.0}
            return out
        m["by_direction"] = _bucketize(lambda t: t.direction.value)
        m["by_session"] = _bucketize(lambda t: t.setup.session.value)
        m["by_model"] = _bucketize(lambda t: t.setup.model.value)
        m["by_grade"] = _bucketize(lambda t: t.setup.grade.value)
        m["by_regime"] = _bucketize(lambda t: t.setup.regime.value)
        m["by_weekday"] = _bucketize(
            lambda t: t.entry_time.strftime("%a") if t.entry_time else "?")
        m["by_tf_combo"] = _bucketize(
            lambda t: f"{t.setup.tf_plan.bias_tf.value}/"
                      f"{t.setup.tf_plan.decision_tf.value}/"
                      f"{t.setup.tf_plan.entry_tf.value}")
        atrs = sorted(t.setup.atr for t in closed if t.setup.atr > 0)
        if atrs:
            t1 = atrs[len(atrs) // 3] if len(atrs) >= 3 else atrs[0]
            t2 = atrs[2 * len(atrs) // 3] if len(atrs) >= 3 else atrs[-1]

            def _vol_bucket(t: Trade) -> str:
                if t.setup.atr <= t1:
                    return "low_vol"
                if t.setup.atr <= t2:
                    return "mid_vol"
                return "high_vol"
            m["by_volatility"] = _bucketize(_vol_bucket)
        # by month, for concentration analysis
        m["by_month"] = _bucketize(
            lambda t: t.entry_time.strftime("%Y-%m") if t.entry_time else "?")
        months = m["by_month"]
        if months and m["net_profit"] > 0:
            top = max(v["net"] for v in months.values())
            m["profit_concentration"] = round(top / m["net_profit"], 3) \
                if m["net_profit"] > 0 else 0.0
        else:
            m["profit_concentration"] = 0.0
        return m


class ReportGenerator:
    """Human-readable daily/weekly/monthly and review reports from the DB."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    def performance_report(self, metrics: Dict[str, Any], title: str) -> str:
        lines = [f"===== {title} =====",
                 f"trades={metrics.get('trades', 0)} "
                 f"net={metrics.get('net_profit', 0.0):.2f} "
                 f"({metrics.get('net_return', 0.0):+.2%}) "
                 f"PF={metrics.get('profit_factor', 0.0):.2f} "
                 f"win%={metrics.get('win_rate', 0.0):.1%}",
                 f"expectancy={metrics.get('expectancy_r', 0.0):+.3f}R "
                 f"medianR={metrics.get('median_r', 0.0):+.3f} "
                 f"maxDD={metrics.get('max_drawdown', 0.0):.2%} "
                 f"sharpe~={metrics.get('sharpe_like', 0.0):.2f} "
                 f"sortino~={metrics.get('sortino_like', 0.0):.2f}",
                 f"streaks: +{metrics.get('longest_win_streak', 0)} / "
                 f"-{metrics.get('longest_loss_streak', 0)}  "
                 f"exposure={metrics.get('exposure', 0.0):.1%}  "
                 f"profit-concentration={metrics.get('profit_concentration', 0.0):.1%}"]
        for section in ("by_model", "by_session", "by_grade", "by_direction",
                        "by_regime", "by_tf_combo", "by_volatility"):
            data = metrics.get(section)
            if not data:
                continue
            lines.append(f"-- {section}:")
            for k, v in data.items():
                lines.append(f"   {k:<28} n={v['trades']:<4} "
                             f"net={v['net']:>10.2f} win%={v['win_rate']:.0%} "
                             f"avgR={v['avg_r']:+.2f}")
        return "\n".join(lines)

    def review_findings(self, trades: Sequence[Trade]) -> str:
        """Self-review heuristics named in the spec (premature BE, tight
        stops, chasing, countertrend failures...)."""
        closed = [t for t in trades if t.status == TradeStatus.CLOSED]
        if not closed:
            return "no closed trades to review"
        premature_be = [t for t in closed
                        if t.exit_reason == ExitReason.BREAK_EVEN and t.mfe < 1.5]
        tight_stops = [t for t in closed
                       if t.exit_reason == ExitReason.STOP_LOSS
                       and t.mfe >= 1.0]
        countertrend = [t for t in closed
                        if (t.direction == Direction.LONG
                            and t.setup.htf_bias == TrendState.BEARISH)
                        or (t.direction == Direction.SHORT
                            and t.setup.htf_bias == TrendState.BULLISH)]
        ct_losses = [t for t in countertrend if t.profit < 0]
        sweep_trades = [t for t in closed if t.setup.sweep is not None]
        lines = [f"premature break-evens (BE exit with MFE<1.5R): {len(premature_be)}",
                 f"stopped despite reaching +1R first (stop likely tight "
                 f"or management late): {len(tight_stops)}",
                 f"countertrend trades: {len(countertrend)} "
                 f"(losses: {len(ct_losses)})",
                 f"post-sweep trades: {len(sweep_trades)}, "
                 f"win rate {sum(1 for t in sweep_trades if t.profit > 0) / len(sweep_trades):.0%}"
                 if sweep_trades else "post-sweep trades: 0"]
        return "\n".join(lines)

    def daily_report(self, day: str) -> str:
        rows = self.db.query("SELECT start_equity,end_equity,realised,trades,"
                             "wins,losses,max_drawdown,locked FROM daily_stats "
                             "WHERE day=?", (day,))
        if not rows:
            return f"no daily stats for {day}"
        se, ee, re_, n, w, l, dd, lock = rows[0]
        return (f"DAILY REPORT {day}: start={se:.2f} end={ee:.2f} "
                f"realised={re_:+.2f} trades={n} W/L={w}/{l} "
                f"maxDD={dd:.2%} lock={lock}")


# ===========================================================================
# SECTION 27 — SYNTHETIC DATA (mechanics verification only)
# ===========================================================================

class SyntheticDataGenerator:
    """Deterministic synthetic XAUUSD-like M5 candles with regime phases.
    Used for rule verification and engine smoke tests. Synthetic results say
    NOTHING about real-market profitability — the engine prints that warning
    whenever synthetic data is used."""

    def __init__(self, seed: int = 42, start_price: float = 2400.0):
        self.rng = random.Random(seed)
        self.start_price = start_price

    def generate(self, days: int = 30,
                 start: Optional[datetime] = None) -> List[Candle]:
        rng = self.rng
        start = start or datetime(2025, 1, 6, 0, 0, tzinfo=UTC)  # a Monday
        candles: List[Candle] = []
        price = self.start_price
        t = start
        phase_left = 0
        drift = 0.0
        vol = 1.0
        while len(candles) < days * 288:
            if t.weekday() >= 5:            # market closed on weekends
                t += timedelta(minutes=5)
                continue
            if phase_left <= 0:
                phase = rng.choice(["trend_up", "trend_dn", "range",
                                    "range", "expansion"])
                phase_left = rng.randint(250, 900)
                if phase == "trend_up":
                    drift, vol = +0.045, 1.0
                elif phase == "trend_dn":
                    drift, vol = -0.045, 1.0
                elif phase == "expansion":
                    drift, vol = rng.choice([-0.08, 0.08]), 2.0
                else:
                    drift, vol = 0.0, 0.65
            phase_left -= 1
            hour = t.hour
            sess_vol = 1.2 if 7 <= hour < 21 else 0.55   # session effect
            sigma = 0.9 * vol * sess_vol
            o = price
            ret = rng.gauss(drift * sess_vol, sigma)
            c = max(1.0, o + ret)
            wick_up = abs(rng.gauss(0, sigma * 0.6))
            wick_dn = abs(rng.gauss(0, sigma * 0.6))
            # occasional stop-run wick (liquidity sweep material)
            if rng.random() < 0.02:
                if rng.random() < 0.5:
                    wick_up += sigma * rng.uniform(1.5, 3.0)
                else:
                    wick_dn += sigma * rng.uniform(1.5, 3.0)
            h = max(o, c) + wick_up
            l = min(o, c) - wick_dn
            spread = max(15.0, rng.gauss(30.0, 6.0))
            if rng.random() < 0.004:                     # spread spike
                spread *= rng.uniform(2.0, 4.0)
            candles.append(Candle(t, round(o, 2), round(h, 2), round(l, 2),
                                  round(c, 2), float(rng.randint(50, 500)),
                                  round(spread, 1)))
            price = c
            t += timedelta(minutes=5)
        return candles


# ===========================================================================
# SECTION 28 — CONTEXT BUILDER (shared by backtest and live loop)
# ===========================================================================

class ContextBuilder:
    """Builds an AnalysisContext from base-TF candles. Every series is made
    of COMPLETED candles only (resample enforces bucket completeness)."""

    TF_WINDOW = {Timeframe.M1: 700, Timeframe.M5: 600, Timeframe.M15: 450,
                 Timeframe.M30: 350, Timeframe.H1: 320, Timeframe.H4: 260,
                 Timeframe.D1: 220, Timeframe.W1: 120}

    def __init__(self, cfg: Config, engine: StrategyEngine,
                 sessions: SessionManager, news: NewsFilter):
        self.cfg = cfg
        self.engine = engine
        self.sessions = sessions
        self.news = news

    def series_from_base(self, base: List[Candle],
                         base_tf: Timeframe) -> Dict[Timeframe, List[Candle]]:
        now = base[-1].time + timedelta(minutes=base_tf.minutes)
        out: Dict[Timeframe, List[Candle]] = {}
        for tf_name in self.cfg.analysis_timeframes:
            tf = Timeframe(tf_name)
            if tf.minutes < base_tf.minutes:
                continue                       # cannot build finer than base
            if tf == base_tf:
                series = list(base)
            else:
                series = resample(base, tf, completed_only=True, now=now)
            window = self.TF_WINDOW.get(tf, 300)
            out[tf] = series[-window:]
        return out

    def build(self, series: Dict[Timeframe, List[Candle]],
              spread_points: float, point: float,
              now: datetime) -> Optional[AnalysisContext]:
        cfg = self.cfg
        counts = {tf: len(cs) for tf, cs in series.items()}
        usable = {tf for tf, n in counts.items()
                  if n >= min(cfg.min_candles_required, 60)}
        # preliminary structure TF for regime: H1 if present else biggest usable
        pre_tf = Timeframe.H1 if Timeframe.H1 in usable else None
        if pre_tf is None:
            intraday = [tf for tf in (Timeframe.M30, Timeframe.M15, Timeframe.M5)
                        if tf in usable]
            if not intraday:
                return None
            pre_tf = intraday[0]
        pre_candles = series[pre_tf]
        pre_struct = StructureAnalyzer(cfg).analyze(pre_candles)
        news_blocked, news_reason = self.news.blackout(now)
        pre_regime = MarketRegimeDetector(cfg).classify(
            pre_candles, pre_struct, spread_points, point, news_blocked)

        # ATR percentile & M1 noise for the selector
        atr_all = atr_series(pre_candles, cfg.atr_period)
        atr_pct = percentile_rank(atr_all[-200:], atr_all[-1]) if atr_all else 0.5
        atr_points = atr_all[-1] / point if point > 0 and atr_all else 0.0
        m1_eff = efficiency_ratio(series.get(Timeframe.M1, []), 30)
        session = self.sessions.session_at(now)
        plan = TimeframeSelector(cfg).select(
            counts, atr_pct, spread_points, atr_points, session,
            pre_regime.regime, m1_eff)

        # build TF analyses for plan TFs + HTF zone set
        needed = {plan.bias_tf, plan.structure_tf, plan.decision_tf,
                  plan.entry_tf, plan.management_tf,
                  Timeframe.H1, Timeframe.H4, Timeframe.D1}
        day = now.astimezone(UTC).date()
        marks = self.sessions.marks_for(day)
        tfs: Dict[Timeframe, TFAnalysis] = {}
        for tf in needed:
            cs = series.get(tf)
            if not cs or len(cs) < cfg.atr_period + 10:
                continue
            external = tf in (Timeframe.H4, Timeframe.D1)
            tfs[tf] = self.engine.build_tf_analysis(
                tf, cs, marks if tf == plan.decision_tf else None,
                external=external)
        if plan.decision_tf not in tfs or plan.structure_tf not in tfs:
            return None
        # final regime on the plan's structure timeframe
        sfa = tfs[plan.structure_tf]
        regime = MarketRegimeDetector(cfg).classify(
            sfa.candles, sfa.structure, spread_points, point, news_blocked)
        return AnalysisContext(
            time=now, price=series[plan.decision_tf][-1].close
            if plan.decision_tf in series else pre_candles[-1].close,
            spread_points=spread_points, point=point, tf_plan=plan,
            regime=regime, session=session, news_blocked=news_blocked,
            news_reason=news_reason,
            news_complete=self.news.protection_complete(),
            tfs=tfs, asian_range=self.sessions.asian_range(day))


# ===========================================================================
# SECTION 29 — BACKTEST ENGINE
# ===========================================================================

@dataclass
class BacktestResult:
    run_id: str
    trades: List[Trade]
    equity_curve: List[Tuple[datetime, float]]
    metrics: Dict[str, Any]
    rejections: int
    signals: int
    start: datetime
    end: datetime
    assumptions: List[str]


class BacktestEngine:
    """Event-driven candle backtest with no look-ahead.

    Per completed base candle i (chronological):
      1. queued market orders fill at candle i OPEN (+ half-spread + slip);
      2. pending limit orders fill if candle i traded through the limit;
      3. open trades: SL/TP monitoring inside candle i
         (ASSUMPTION: stop fills first when both SL and TP are inside one
          candle; gap opens fill at the open — conservative, documented);
      4. trade management on each completed management-TF candle;
      5. signal evaluation on each completed decision-TF candle close —
         orders generated here execute from candle i+1 onwards.
    """

    def __init__(self, cfg: Config, db: DatabaseManager,
                 spec: Optional[SymbolSpecification] = None,
                 quiet: bool = False):
        self.cfg = cfg
        self.db = db
        self.spec = spec or default_xauusd_spec()
        self.quiet = quiet

    def run(self, candles: List[Candle], data_desc: str = "csv",
            synthetic: bool = False) -> BacktestResult:
        cfg = self.cfg
        spec = self.spec
        base_tf = Timeframe(cfg.backtest_base_timeframe)
        if synthetic and not self.quiet:
            log.warning("SYNTHETIC DATA: results verify engine mechanics "
                        "only and say NOTHING about real profitability")
        if len(candles) < cfg.backtest_warmup_candles + 100:
            raise ValueError(f"not enough candles: {len(candles)} "
                             f"(need > {cfg.backtest_warmup_candles + 100})")
        engine = StrategyEngine(cfg)
        sessions = SessionManager(cfg)
        news = NewsFilter(cfg)
        builder = ContextBuilder(cfg, engine, sessions, news)
        execu = ExecutionManager(cfg, self.db)
        sizer = PositionSizer(cfg)
        risk = RiskManager(cfg, DatabaseManager(":memory:"))
        tmgr = TradeManager(cfg)

        equity = cfg.backtest_initial_equity
        curve: List[Tuple[datetime, float]] = []
        market_queue: List[Tuple[Setup, SizingResult]] = []
        rejections = 0
        signals = 0
        last_decision_bucket: Optional[datetime] = None
        last_mgmt_bucket: Optional[datetime] = None
        decision_tf = Timeframe.M15
        mgmt_tf = Timeframe(cfg.trail_timeframe)
        warm = cfg.backtest_warmup_candles

        for c in candles[:warm]:
            sessions.update_ranges(c)

        def reject_cb_factory(now: datetime):
            def _cb(model: str, stage: str, reason: str) -> None:
                nonlocal rejections
                rejections += 1
                self.db.journal_rejection(now, "", model, "", 0.0, stage, reason)
            return _cb

        n = len(candles)
        for i in range(warm, n):
            c = candles[i]
            now = c.time + timedelta(minutes=base_tf.minutes)  # candle close
            sessions.update_ranges(c)
            spread_pts = c.spread if c.spread > 0 else cfg.backtest_spread_points
            risk.roll(now, equity)

            # 1. queued market orders fill at this candle's open
            if market_queue:
                slip = cfg.slippage_buffer_points * spec.point
                spr = spread_pts * spec.point
                for setup, sizing in market_queue:
                    tr = Trade(trade_id=new_id("trade"), setup=setup,
                               status=TradeStatus.OPEN, volume=sizing.volume,
                               initial_volume=sizing.volume,
                               risk_fraction=sizing.risk_fraction_actual,
                               risk_money=sizing.risk_money,
                               entry_price=c.open + setup.direction.sign * (spr / 2 + slip),
                               entry_time=c.time,
                               stop_price=setup.stop_price,
                               initial_stop=setup.stop_price,
                               tp1=setup.tp1, tp2=setup.tp2)
                    tr.commission = cfg.commission_per_lot * sizing.volume
                    execu.open_trades.append(tr)
                    risk.register_open(now, setup.session)
                market_queue.clear()

            # 2. limit fills, 3. SL/TP monitoring on this candle
            for tr in execu.paper_check_pending(c, spec, now):
                risk.register_open(now, tr.setup.session)
            for tr in execu.paper_update_open(c, spec, now):
                equity += tr.profit
                risk.register_close(tr.profit)
                self.db.journal_trade(tr, Mode.BACKTEST, spec.name)

            # 4. management on completed mgmt-TF candle
            mb = tf_bucket_start(c.time, mgmt_tf)
            if execu.open_trades and mb != last_mgmt_bucket and i > warm:
                last_mgmt_bucket = mb
                mgmt_series = resample(candles[max(0, i - 2200):i + 1], mgmt_tf,
                                       completed_only=True, now=now)
                if len(mgmt_series) > cfg.atr_period + 10:
                    mfa = engine.build_tf_analysis(mgmt_tf,
                                                   mgmt_series[-350:])
                    news_block, _ = news.blackout(now)
                    weekend = sessions.near_weekend_flat(now)
                    for tr in list(execu.open_trades):
                        for act in tmgr.manage(tr, mfa, now, spec,
                                               news_exit=False,
                                               weekend_flat=weekend):
                            execu.paper_apply_action(act, spec, now)
                            if act.kind == "CLOSE":
                                equity += tr.profit
                                risk.register_close(tr.profit)
                                self.db.journal_trade(tr, Mode.BACKTEST, spec.name)

            # 5. signal evaluation on completed decision-TF candle
            db_ = tf_bucket_start(c.time, decision_tf)
            new_decision_candle = db_ != last_decision_bucket
            if new_decision_candle:
                last_decision_bucket = db_
                base_slice = candles[max(0, i - 6000):i + 1]
                series = builder.series_from_base(base_slice, base_tf)
                ctx = builder.build(series, spread_pts, spec.point, now)
                if ctx is not None:
                    decision_tf = ctx.tf_plan.decision_tf
                    mgmt_tf = ctx.tf_plan.management_tf
                    allowed, why = sessions.entry_window_check(now)
                    unreal = execu.paper_unrealised(c.close, spec)
                    lock = risk.lock_reason(now, equity, unreal, ctx.session)
                    can_new = (allowed and lock == LockReason.NONE
                               and not ctx.news_blocked
                               and spread_pts <= cfg.max_spread_points
                               and len(execu.open_trades)
                               + len(execu.pending_trades) < cfg.max_positions)
                    if can_new:
                        money_per_unit = spec.money_per_price_unit_per_lot()
                        cost = (spread_pts + cfg.slippage_buffer_points) * spec.point \
                            + (cfg.commission_per_lot / money_per_unit
                               if money_per_unit > 0 else 0.0)
                        setup = engine.evaluate(ctx, cost,
                                                reject_cb_factory(now))
                        if setup is not None:
                            signals += 1
                            grade_risk = sizer.risk_fraction_for(setup.grade,
                                                                 setup.score)
                            grade_risk = risk.adjusted_risk(grade_risk, unreal)
                            sizing = sizer.size(spec, equity, grade_risk,
                                                setup.entry_price,
                                                setup.stop_price)
                            ok_open, why_open = risk.can_open(
                                now, equity, len(execu.open_trades),
                                execu.open_risk_money(), sizing.risk_money,
                                unreal, ctx.session)
                            if sizing.rejected or not ok_open \
                                    or execu.duplicate(setup) \
                                    or execu.conflicting(setup):
                                reason = sizing.reason or why_open
                                if execu.duplicate(setup):
                                    reason = "duplicate setup"
                                if execu.conflicting(setup):
                                    reason = "conflicting open position"
                                self.db.journal_rejection(
                                    now, setup.setup_id, setup.model.value,
                                    setup.direction.value, setup.score,
                                    "risk/size", reason)
                                self.db.journal_signal(setup, False,
                                                       Mode.BACKTEST, spec.name)
                                rejections += 1
                            else:
                                self.db.journal_signal(setup, True,
                                                       Mode.BACKTEST, spec.name)
                                if setup.entry_mode == EntryMode.LIMIT_ON_RETEST:
                                    execu.paper_submit(setup, sizing, spec, now)
                                else:
                                    market_queue.append((setup, sizing))
            unreal = execu.paper_unrealised(c.close, spec)
            curve.append((now, equity + unreal))

        # close anything left at the final price
        final = candles[-1]
        for tr in list(execu.open_trades):
            execu.paper_apply_action(
                ManagementAction("CLOSE", tr, price=final.close,
                                 reason=ExitReason.END_OF_DATA), spec,
                final.time)
            equity += tr.profit
            self.db.journal_trade(tr, Mode.BACKTEST, spec.name)
        for tr in list(execu.pending_trades):
            tr.status = TradeStatus.CANCELLED
            execu.pending_trades.remove(tr)
            execu.closed_trades.append(tr)

        all_trades = execu.closed_trades
        metrics = PerformanceAnalyzer.metrics(all_trades, curve,
                                              cfg.backtest_initial_equity)
        metrics["signals"] = signals
        metrics["rejections"] = rejections
        metrics["synthetic_data"] = synthetic
        run_id = new_id("bt")
        self.db.record_backtest(run_id, data_desc, cfg.config_hash(),
                                candles[warm].time.isoformat(),
                                candles[-1].time.isoformat(), metrics)
        assumptions = [
            "stop fills before target when both are inside one candle (no tick data)",
            "gaps through stops/targets fill at the candle open",
            f"market entries fill at next candle open + half-spread + "
            f"{cfg.slippage_buffer_points:.0f}pt slippage",
            f"fixed commission {cfg.commission_per_lot:.2f}/lot round turn",
            "limit orders fill when the candle trades through the price "
            "(fills that depend on queue position are treated as filled)",
        ]
        if not any(cd.spread > 0 for cd in candles[:200]):
            assumptions.append(
                f"CSV had no spread column: fixed {cfg.backtest_spread_points:.0f}pt spread assumed")
        return BacktestResult(run_id, all_trades, curve, metrics, rejections,
                              signals, candles[warm].time, candles[-1].time,
                              assumptions)


# ===========================================================================
# SECTION 30 — WALK-FORWARD + MONTE CARLO
# ===========================================================================

class WalkForwardEngine:
    """Anchored walk-forward: for each fold, optimise a SMALL parameter grid
    on the train segment, confirm on validation, then measure out-of-sample.
    The grid is deliberately tiny (broad, understandable parameters) to
    avoid overfitting; the engine reports parameter sensitivity so fragile
    settings are visible."""

    GRID: Tuple[Dict[str, float], ...] = (
        {"min_score": 70.0, "displacement_atr_mult": 1.0},
        {"min_score": 70.0, "displacement_atr_mult": 1.2},
        {"min_score": 75.0, "displacement_atr_mult": 1.2},
        {"min_score": 75.0, "displacement_atr_mult": 1.4},
        {"min_score": 80.0, "displacement_atr_mult": 1.2},
    )

    def __init__(self, cfg: Config, db: DatabaseManager):
        self.cfg = cfg
        self.db = db

    def run(self, candles: List[Candle], synthetic: bool = False) -> Dict[str, Any]:
        cfg = self.cfg
        n = len(candles)
        folds = max(1, cfg.wf_folds)
        fold_results: List[Dict[str, Any]] = []
        test_frac = 1.0 - cfg.wf_train_fraction - cfg.wf_validate_fraction
        seg = n // folds
        for f in range(folds):
            lo = 0                       # anchored: train always starts at 0
            hi = seg * (f + 1)
            if hi - lo < cfg.backtest_warmup_candles + 800:
                continue
            chunk = candles[lo:hi]
            m = len(chunk)
            tr_end = int(m * cfg.wf_train_fraction)
            va_end = int(m * (cfg.wf_train_fraction + cfg.wf_validate_fraction))
            train = chunk[:tr_end]
            validate = chunk[:va_end]     # engines need warmup: use prefix
            test = chunk                  # OOS measured on the final part
            best_params: Optional[Dict[str, float]] = None
            best_score = -1e18
            sensitivity: List[Dict[str, Any]] = []
            for params in self.GRID:
                res = self._run_with(train, params, synthetic)
                sc = self._objective(res.metrics)
                sensitivity.append({"params": params,
                                    "trades": res.metrics.get("trades", 0),
                                    "expectancy_r": res.metrics.get("expectancy_r", 0.0),
                                    "profit_factor": res.metrics.get("profit_factor", 0.0),
                                    "objective": round(sc, 4)})
                if sc > best_score:
                    best_score = sc
                    best_params = params
            va_res = self._run_with(validate, best_params or {}, synthetic)
            oos_res = self._run_with(test, best_params or {}, synthetic)
            oos_tail = self._tail_metrics(oos_res, start_frac=cfg.wf_train_fraction
                                          + cfg.wf_validate_fraction)
            fold_results.append({
                "fold": f + 1, "candles": m, "chosen": best_params,
                "train_expectancy_r": self._run_metric(sensitivity, best_params),
                "validate_expectancy_r": va_res.metrics.get("expectancy_r", 0.0),
                "validate_trades": va_res.metrics.get("trades", 0),
                "oos_expectancy_r": oos_tail.get("expectancy_r", 0.0),
                "oos_trades": oos_tail.get("trades", 0),
                "oos_profit_factor": oos_tail.get("profit_factor", 0.0),
                "sensitivity": sensitivity})
        summary = {"folds": fold_results,
                   "stable": self._stability(fold_results),
                   "warning": ("synthetic data — mechanics check only"
                               if synthetic else "")}
        self.db.execute(
            "INSERT OR REPLACE INTO walkforward_runs VALUES (?,?,?,?)",
            (new_id("wf"), utcnow().isoformat(), len(fold_results),
             json.dumps(summary, default=str)))
        return summary

    def _run_with(self, candles: List[Candle], params: Dict[str, float],
                  synthetic: bool) -> BacktestResult:
        cfg2 = replace(self.cfg)
        for k, v in params.items():
            setattr(cfg2, k, v)
        cfg2.min_score = max(70.0, getattr(cfg2, "min_score", 70.0))
        engine = BacktestEngine(cfg2, DatabaseManager(":memory:"),
                                quiet=True)
        return engine.run(candles, data_desc="wf-segment",
                          synthetic=synthetic)

    @staticmethod
    def _run_metric(sensitivity: List[Dict[str, Any]],
                    params: Optional[Dict[str, float]]) -> float:
        for row in sensitivity:
            if row["params"] == params:
                return row["expectancy_r"]
        return 0.0

    @staticmethod
    def _objective(m: Dict[str, Any]) -> float:
        """Expectancy weighted by sample size, penalised by drawdown.
        Never raw net profit."""
        t = m.get("trades", 0)
        if t < 5:
            return -1e9
        return (m.get("expectancy_r", 0.0) * math.sqrt(t)
                - 2.0 * m.get("max_drawdown", 0.0))

    def _tail_metrics(self, res: BacktestResult,
                      start_frac: float) -> Dict[str, Any]:
        """Metrics restricted to trades in the OOS tail of the segment."""
        if not res.trades:
            return {"trades": 0, "expectancy_r": 0.0, "profit_factor": 0.0}
        t0 = res.start + (res.end - res.start) * start_frac
        tail = [t for t in res.trades
                if t.entry_time and t.entry_time >= t0
                and t.status == TradeStatus.CLOSED]
        curve = [(t, e) for t, e in res.equity_curve if t >= t0]
        base = curve[0][1] if curve else self.cfg.backtest_initial_equity
        return PerformanceAnalyzer.metrics(tail, curve, base)

    @staticmethod
    def _stability(folds: List[Dict[str, Any]]) -> bool:
        if not folds:
            return False
        oos = [f["oos_expectancy_r"] for f in folds if f["oos_trades"] >= 3]
        if not oos:
            return False
        return all(e > -0.1 for e in oos)


class MonteCarloAnalyzer:
    """Trade-sequence reshuffling: distribution of equity paths and
    drawdowns if the same trades had arrived in a different order."""

    def __init__(self, runs: int = 1000, seed: int = 7):
        self.runs = runs
        self.rng = random.Random(seed)

    def analyze(self, trades: Sequence[Trade],
                initial_equity: float) -> Dict[str, Any]:
        rs = [t.r_multiple() for t in trades
              if t.status == TradeStatus.CLOSED and t.risk_money > 0]
        profits = [t.profit for t in trades if t.status == TradeStatus.CLOSED]
        if len(profits) < 10:
            return {"runs": 0, "note": "fewer than 10 closed trades — "
                    "Monte Carlo not meaningful"}
        finals: List[float] = []
        dds: List[float] = []
        for _ in range(self.runs):
            seq = profits[:]
            self.rng.shuffle(seq)
            eq = initial_equity
            peak = eq
            dd = 0.0
            for p in seq:
                eq += p
                peak = max(peak, eq)
                if peak > 0:
                    dd = max(dd, (peak - eq) / peak)
            finals.append(eq)
            dds.append(dd)
        finals.sort()
        dds.sort()

        def pct(arr: List[float], q: float) -> float:
            return arr[min(len(arr) - 1, int(q * len(arr)))]
        return {
            "runs": self.runs,
            "trades_per_run": len(profits),
            "final_equity_p5": round(pct(finals, 0.05), 2),
            "final_equity_p50": round(pct(finals, 0.50), 2),
            "final_equity_p95": round(pct(finals, 0.95), 2),
            "max_dd_p50": round(pct(dds, 0.50), 4),
            "max_dd_p95": round(pct(dds, 0.95), 4),
            "prob_ruin_20pct": round(sum(1 for d in dds if d >= 0.20)
                                     / len(dds), 4),
            "avg_r": round(statistics.mean(rs), 3) if rs else 0.0,
        }


# ===========================================================================
# SECTION 31 — LIVE-TRADING GATE
# ===========================================================================

class LiveGate:
    """Every condition that must hold before LIVE mode is allowed.
    Live trading can never be enabled automatically."""

    def __init__(self, cfg: Config, db: DatabaseManager,
                 cli_flag: bool):
        self.cfg = cfg
        self.db = db
        self.cli_flag = cli_flag

    def check(self) -> Tuple[bool, List[str]]:
        c = self.cfg
        failures: List[str] = []
        if c.mode != Mode.LIVE:
            failures.append("mode is not LIVE")
        if not c.live_trading_enabled:
            failures.append("LIVE_TRADING_ENABLED is False (edit config "
                            "deliberately to enable)")
        if not self.cli_flag:
            failures.append("--i-understand-live-risk flag not provided")
        if c.live_account_number <= 0:
            failures.append("live_account_number not configured")
        if not c.live_server:
            failures.append("live_server not configured")
        if not c.backtest_verified:
            failures.append("backtest_verified is False — complete and "
                            "review backtesting first")
        if not c.paper_verified:
            failures.append("paper_verified is False — complete paper phase")
        if not c.demo_verified:
            failures.append("demo_verified is False — complete demo phase")
        # research standards from recorded backtests
        rows = self.db.query(
            "SELECT trades, profit_factor, max_dd FROM backtest_runs "
            "ORDER BY time DESC LIMIT 5")
        if not rows:
            failures.append("no recorded backtest runs in the database")
        else:
            ok = any(r[0] >= c.live_min_trades
                     and r[1] >= c.live_min_profit_factor
                     and r[2] <= c.live_max_drawdown for r in rows)
            if not ok:
                failures.append(
                    f"no recent backtest meets the standards "
                    f"(>= {c.live_min_trades} trades, PF >= "
                    f"{c.live_min_profit_factor}, maxDD <= "
                    f"{c.live_max_drawdown:.0%})")
        validator = ConfigValidator(c)
        if not validator.validate():
            failures.extend(validator.errors)
        return (len(failures) == 0, failures)


# ===========================================================================
# SECTION 32 — BOT CONTROLLER (PAPER / DEMO / LIVE loop)
# ===========================================================================

class BotController:
    """Main runtime loop. Signals are evaluated on completed decision-TF
    candles from the MT5 feed; ticks are used only for executing confirmed
    signals and protective monitoring."""

    def __init__(self, cfg: Config, cli_live_flag: bool = False):
        self.cfg = cfg
        self.db = DatabaseManager(cfg.db_path)
        self.db.record_config(cfg, f"startup mode={cfg.mode.value}")
        self.connector = MT5Connector(cfg)
        self.symbols = SymbolManager(cfg, self.connector)
        self.data = MarketDataManager(cfg, self.connector, self.symbols)
        self.engine = StrategyEngine(cfg)
        self.sessions = SessionManager(cfg)
        self.news = NewsFilter(cfg)
        self.builder = ContextBuilder(cfg, self.engine, self.sessions, self.news)
        self.execu = ExecutionManager(cfg, self.db, self.connector, self.symbols)
        self.sizer = PositionSizer(cfg)
        self.risk = RiskManager(cfg, self.db)
        self.tmgr = TradeManager(cfg)
        self.health = HealthMonitor(cfg, self.db)
        self.notify = TelegramNotifier(cfg)
        self.cli_live_flag = cli_live_flag
        self._stop = threading.Event()
        self._paper_equity: float = float(
            self.db.get_state("paper_equity", cfg.backtest_initial_equity)
            or cfg.backtest_initial_equity)
        self._last_decision_bucket: Optional[datetime] = None
        self._last_mgmt_bucket: Optional[datetime] = None
        self._decision_tf = Timeframe.M15
        self._mgmt_tf = Timeframe(cfg.trail_timeframe)
        self._base_tf = Timeframe.M5

    # ------------------------------------------------------------ lifecycle
    def start(self) -> int:
        cfg = self.cfg
        validator = ConfigValidator(cfg)
        if not validator.validate():
            print(validator.report())
            return 2
        if validator.warnings:
            print(validator.report())
        if cfg.mode == Mode.LIVE:
            gate = LiveGate(cfg, self.db, self.cli_live_flag)
            ok, failures = gate.check()
            if not ok:
                log.critical("LIVE GATE REFUSED:")
                for f in failures:
                    log.critical("  - %s", f)
                return 3
            log.warning("LIVE GATE PASSED — live trading will use real money")
        if cfg.mode == Mode.BACKTEST:
            log.error("BotController does not run BACKTEST mode; use "
                      "--mode BACKTEST via main()")
            return 2
        if not MT5_AVAILABLE:
            log.critical("MetaTrader5 package unavailable on this platform "
                         "(Windows only). %s mode needs a live MT5 feed. "
                         "BACKTEST and --test work everywhere.",
                         cfg.mode.value)
            return 4
        if not self.connector.connect():
            return 5
        ok, why = self.connector.verify_account(cfg.mode)
        if not ok:
            log.critical("account verification failed: %s", why)
            self.connector.shutdown()
            return 6
        if self.symbols.detect() is None:
            self.connector.shutdown()
            return 7
        self.execu.reconcile()               # restart recovery
        self.notify.event(f"bot started — mode={cfg.mode.value} "
                          f"symbol={self.symbols.spec.name} "
                          f"v{BOT_VERSION}")
        for sig in (os_signal.SIGINT, os_signal.SIGTERM):
            try:
                os_signal.signal(sig, lambda *_: self._stop.set())
            except (ValueError, OSError):
                pass
        try:
            self._loop()
        except Exception as exc:
            log.critical("fatal loop error: %s", exc)
            self.db.journal_error("controller", str(exc),
                                  traceback.format_exc(), "CRITICAL")
            self.notify.event("CRITICAL ERROR — bot halted", str(exc))
            return 10
        finally:
            self._persist()
            self.notify.event("bot stopped")
            self.connector.shutdown()
            self.db.close()
        return 0

    def stop(self) -> None:
        self._stop.set()

    def _persist(self) -> None:
        self.db.set_state("paper_equity", self._paper_equity)

    def _equity(self) -> float:
        if self.cfg.mode == Mode.PAPER:
            return self._paper_equity
        eq = self.connector.equity()
        return eq if eq is not None else 0.0

    # ------------------------------------------------------------ main loop
    def _loop(self) -> None:
        cfg = self.cfg
        spec = self.symbols.spec
        seen_candle: Optional[datetime] = None
        history = self.data.candles(self._base_tf, 6000)
        self.sessions.rebuild_from(history)
        disconnected_since: Optional[datetime] = None

        while not self._stop.is_set():
            lock = self.health.check(self.connector, self.data, self._base_tf)
            if lock == LockReason.KILL_SWITCH:
                log.critical("KILL SWITCH detected — halting safely")
                self.notify.event("kill switch — bot halting")
                break
            if lock == LockReason.DISCONNECTED:
                if disconnected_since is None:
                    disconnected_since = utcnow()
                    log.error("terminal disconnected — entering safe wait")
                    self.notify.event("connection lost")
                time_mod.sleep(5)
                if self.connector.connect():
                    disconnected_since = None
                    self.execu.reconcile()
                    self.notify.event("connection restored")
                continue
            equity = self._equity()
            self.health.heartbeat(cfg.mode, equity,
                                  len(self.execu.open_trades),
                                  lock.value)
            if lock == LockReason.STALE_DATA:
                time_mod.sleep(cfg.loop_interval_seconds)
                continue

            candles = self.data.candles(self._base_tf, 6000)
            if not candles:
                time_mod.sleep(cfg.loop_interval_seconds)
                continue
            newest = candles[-1].time
            if seen_candle is not None and newest == seen_candle:
                # no new completed candle: protective tick monitoring only
                self._tick_protection(spec)
                time_mod.sleep(cfg.loop_interval_seconds)
                continue
            seen_candle = newest
            c = candles[-1]
            now = c.time + timedelta(minutes=self._base_tf.minutes)
            self.sessions.update_ranges(c)
            self.risk.roll(now, equity)
            spread_pts = self.symbols.refresh_spread() or spec.spread_points

            # paper fills & protective checks on the completed candle
            if cfg.mode == Mode.PAPER:
                self.execu.paper_check_pending(c, spec, now)
                for tr in self.execu.paper_update_open(c, spec, now):
                    self._on_close(tr)

            # management on completed mgmt-TF candles
            mb = tf_bucket_start(c.time, self._mgmt_tf)
            if self.execu.open_trades and mb != self._last_mgmt_bucket:
                self._last_mgmt_bucket = mb
                self._manage(candles, now, spec)

            # decision evaluation on completed decision-TF candles
            db_ = tf_bucket_start(c.time, self._decision_tf)
            if db_ != self._last_decision_bucket:
                self._last_decision_bucket = db_
                self._evaluate(candles, now, spec, spread_pts, equity)
            time_mod.sleep(cfg.loop_interval_seconds)

    def _tick_protection(self, spec: SymbolSpecification) -> None:
        """Between candles: emergency protection only (paper mode; MT5 holds
        server-side SL/TP for demo/live)."""
        if self.cfg.mode != Mode.PAPER or not self.execu.open_trades:
            return
        tick = self.data.tick_price()
        if tick is None:
            return
        bid, ask = tick
        now = utcnow()
        for tr in list(self.execu.open_trades):
            px = bid if tr.direction == Direction.LONG else ask
            hit = px <= tr.stop_price if tr.direction == Direction.LONG \
                else px >= tr.stop_price
            if hit:
                self.execu.paper_apply_action(
                    ManagementAction("CLOSE", tr, price=tr.stop_price,
                                     reason=ExitReason.STOP_LOSS),
                    spec, now)
                self._on_close(tr)

    def _on_close(self, tr: Trade) -> None:
        self._paper_equity += tr.profit if self.cfg.mode == Mode.PAPER else 0.0
        self.risk.register_close(tr.profit)
        self.db.journal_trade(tr, self.cfg.mode, self.symbols.spec.name)
        self._persist()
        self.notify.event(
            f"trade closed {tr.trade_id[-6:]} {tr.direction.value} "
            f"{tr.exit_reason.value if tr.exit_reason else ''} "
            f"P/L {tr.profit:+.2f} ({tr.r_multiple():+.2f}R)")

    def _manage(self, candles: List[Candle], now: datetime,
                spec: SymbolSpecification) -> None:
        cfg = self.cfg
        mgmt_series = resample(candles, self._mgmt_tf, completed_only=True,
                               now=now)
        if len(mgmt_series) <= cfg.atr_period + 10:
            return
        mfa = self.engine.build_tf_analysis(self._mgmt_tf, mgmt_series[-350:])
        news_block, _ = self.news.blackout(now + timedelta(minutes=10))
        weekend = self.sessions.near_weekend_flat(now)
        for tr in list(self.execu.open_trades):
            actions = self.tmgr.manage(tr, mfa, now, spec,
                                       news_exit=False,
                                       weekend_flat=weekend)
            for act in actions:
                if cfg.mode == Mode.PAPER:
                    self.execu.paper_apply_action(act, spec, now)
                    if act.kind == "CLOSE":
                        self._on_close(tr)
                else:
                    if act.kind == "MOVE_STOP":
                        if self.execu.mt5_modify_stop(tr, act.price):
                            self.notify.event(
                                f"stop moved {tr.trade_id[-6:]} -> "
                                f"{act.price:.2f} ({act.note})")
                    elif act.kind == "PARTIAL_CLOSE":
                        if self.execu.mt5_close(tr, act.volume, act.reason):
                            tr.volume = spec.round_volume_down(
                                tr.volume - act.volume)
                            tr.partial_done = True
                            self.notify.event(
                                f"partial close {tr.trade_id[-6:]} "
                                f"{act.volume} lots ({act.note})")
                    elif act.kind == "CLOSE":
                        if self.execu.mt5_close(tr, reason=act.reason):
                            tr.status = TradeStatus.CLOSED
                            tr.exit_time = now
                            tr.exit_reason = act.reason
                            if tr in self.execu.open_trades:
                                self.execu.open_trades.remove(tr)
                            self.execu.closed_trades.append(tr)
                            self._on_close(tr)

    def _evaluate(self, candles: List[Candle], now: datetime,
                  spec: SymbolSpecification, spread_pts: float,
                  equity: float) -> None:
        cfg = self.cfg
        series = self.builder.series_from_base(candles, self._base_tf)
        ctx = self.builder.build(series, spread_pts, spec.point, now)
        if ctx is None:
            return
        self._decision_tf = ctx.tf_plan.decision_tf
        self._mgmt_tf = ctx.tf_plan.management_tf
        log.info("plan: %s | regime=%s (%s) | session=%s",
                 ctx.tf_plan.as_dict(), ctx.regime.regime.value,
                 ctx.regime.reason, ctx.session.value)
        unreal = self.execu.paper_unrealised(ctx.price, spec) \
            if cfg.mode == Mode.PAPER else 0.0
        lock = self.risk.lock_reason(now, equity, unreal, ctx.session)
        allowed, window_why = self.sessions.entry_window_check(now)

        def reject_cb(model: str, stage: str, reason: str) -> None:
            self.db.journal_rejection(now, "", model, "", 0.0, stage, reason)

        if lock != LockReason.NONE:
            log.info("no new entries: lock %s", lock.value)
            if lock in (LockReason.DAILY_LOSS, LockReason.WEEKLY_LOSS,
                        LockReason.CONSECUTIVE_LOSSES, LockReason.DAILY_TARGET):
                self.notify.event(f"risk lock active: {lock.value}")
            return
        if not allowed:
            log.info("no new entries: %s", window_why)
            return
        if ctx.news_blocked:
            log.info("news blackout: %s", ctx.news_reason)
            return
        if spread_pts > cfg.max_spread_points:
            log.info("spread too high: %.0f pts", spread_pts)
            return
        if len(self.execu.open_trades) + len(self.execu.pending_trades) \
                >= cfg.max_positions:
            return

        money_per_unit = spec.money_per_price_unit_per_lot()
        cost = (spread_pts + cfg.slippage_buffer_points) * spec.point \
            + (cfg.commission_per_lot / money_per_unit
               if money_per_unit > 0 else 0.0)
        setup = self.engine.evaluate(ctx, cost, reject_cb)
        if setup is None:
            return
        grade_risk = self.sizer.risk_fraction_for(setup.grade, setup.score)
        grade_risk = self.risk.adjusted_risk(grade_risk, unreal)
        sizing = self.sizer.size(spec, equity, grade_risk,
                                 setup.entry_price, setup.stop_price)
        signal_age = (utcnow() - now).total_seconds() \
            if cfg.mode != Mode.PAPER else 0.0
        ok, why = self.execu.preflight(
            setup, sizing, cfg.mode, ctx.news_blocked, lock,
            max(0.0, signal_age), ctx.price)
        ok2, why2 = self.risk.can_open(now, equity,
                                       len(self.execu.open_trades),
                                       self.execu.open_risk_money(),
                                       sizing.risk_money, unreal, ctx.session)
        if not (ok and ok2):
            reason = why if not ok else why2
            self.db.journal_rejection(now, setup.setup_id, setup.model.value,
                                      setup.direction.value, setup.score,
                                      "preflight", reason)
            self.db.journal_signal(setup, False, cfg.mode, spec.name)
            self.notify.event(f"setup rejected ({reason})")
            log.info("setup rejected: %s", reason)
            return

        self.db.journal_signal(setup, True, cfg.mode, spec.name)
        plan_text = (f"{setup.direction.value} {spec.name} @ "
                     f"{setup.entry_price:.2f} SL {setup.stop_price:.2f} "
                     f"TP1 {setup.tp1:.2f} TP2 {setup.tp2:.2f} "
                     f"risk {sizing.risk_fraction_actual:.2%} "
                     f"vol {sizing.volume}")
        self.notify.structured(
            immediate=(f"{setup.model.value} {setup.grade.value} "
                       f"score {setup.score:.0f} — {setup.reason}"),
            key_levels=(f"entry {setup.entry_price:.2f} / stop "
                        f"{setup.stop_price:.2f} / targets {setup.tp1:.2f}, "
                        f"{setup.tp2:.2f}"),
            plan=plan_text,
            verdict=(Verdict.WAITING_FOR_RETEST
                     if setup.entry_mode == EntryMode.LIMIT_ON_RETEST
                     else Verdict.LIVE_NOW) if not cfg.alert_only
            else Verdict.WAITING_FOR_CONFIRMATION,
            invalidation=f"close beyond {setup.stop_price:.2f}",
            next_trigger=f"next {ctx.tf_plan.decision_tf.value} candle close")
        if cfg.alert_only:
            log.info("ALERT-ONLY mode: %s", plan_text)
            return
        if cfg.mode == Mode.PAPER:
            trade = self.execu.paper_submit(setup, sizing, spec, now)
            self.risk.register_open(now, ctx.session)
            log.info("PAPER %s: %s", trade.status.value, plan_text)
        else:
            trade = self.execu.mt5_submit(setup, sizing, cfg.mode)
            if trade is not None:
                self.risk.register_open(now, ctx.session)
                self.db.journal_trade(trade, cfg.mode, spec.name)
                log.info("%s order sent: %s", cfg.mode.value, plan_text)
            else:
                self.db.journal_rejection(now, setup.setup_id,
                                          setup.model.value,
                                          setup.direction.value, setup.score,
                                          "execution", "order_send failed")


def run_emergency_close(cfg: Config) -> int:
    """Connect, reconcile and close every position with our magic number."""
    if not MT5_AVAILABLE:
        print("MetaTrader5 package unavailable — nothing to close here.")
        return 1
    connector = MT5Connector(cfg)
    if not connector.connect():
        return 1
    symbols = SymbolManager(cfg, connector)
    if symbols.detect() is None:
        connector.shutdown()
        return 1
    db = DatabaseManager(cfg.db_path)
    execu = ExecutionManager(cfg, db, connector, symbols)
    execu.reconcile()
    execu.emergency_close_all()
    connector.shutdown()
    db.close()
    print("emergency close completed")
    return 0


# ===========================================================================
# SECTION 33 — BUILT-IN TEST SUITE (run with --test)
# ===========================================================================

def _t(i: int) -> datetime:
    """Deterministic 5-minute timestamps for synthetic test candles
    (Mon 2025-01-06 08:00 UTC = London session)."""
    return datetime(2025, 1, 6, 8, 0, tzinfo=UTC) + timedelta(minutes=5 * i)


def _mk(i: int, o: float, h: float, l: float, c: float,
        spread: float = 30.0) -> Candle:
    return Candle(_t(i), o, h, l, c, 100.0, spread)


def _flat(start: int, count: int, price: float, rng: float = 1.0) -> List[Candle]:
    out = []
    for k in range(count):
        out.append(_mk(start + k, price, price + rng / 2, price - rng / 2,
                       price + (0.1 if k % 2 == 0 else -0.1)))
    return out


def _structure_pattern() -> List[Candle]:
    """Hand-built pattern: swing low @2, swing high @5, higher low @8,
    displacement BOS @11, swing high @11, CHoCH/MSS break @17."""
    rows = [
        (100.0, 100.5, 99.5, 100.0),   # 0
        (100.0, 100.4, 99.4, 99.8),    # 1
        (99.8, 99.9, 99.0, 99.5),      # 2  swing low 99.0
        (99.5, 100.6, 99.4, 100.5),    # 3
        (100.5, 101.2, 100.3, 101.0),  # 4
        (101.0, 101.8, 100.8, 101.5),  # 5  swing high 101.8
        (101.5, 101.6, 100.9, 101.0),  # 6
        (101.0, 101.2, 100.4, 100.6),  # 7
        (100.6, 100.8, 100.0, 100.5),  # 8  higher low 100.0 (bearish candle)
        (100.5, 101.3, 100.3, 101.2),  # 9
        (101.2, 101.7, 101.0, 101.6),  # 10
        (101.6, 103.6, 101.4, 103.4),  # 11 displacement close > 101.8 => BOS
        (103.4, 102.8, 102.0, 102.5),  # 12
        (102.5, 102.9, 102.1, 102.4),  # 13
        (102.4, 102.6, 101.9, 102.0),  # 14
        (102.0, 102.2, 101.6, 101.8),  # 15
        (101.8, 101.9, 100.5, 100.7),  # 16
        (100.7, 100.8, 99.4, 99.6),    # 17 close < 100.0 => CHoCH/MSS short
    ]
    return [_mk(i, *r) for i, r in enumerate(rows)]


class TestSwings(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_swing_high_low_detection(self):
        candles = _structure_pattern()
        det = SwingDetector(2, 2)
        swings = det.detect(candles)
        kinds = {(s.index, s.kind) for s in swings}
        self.assertIn((2, SwingKind.LOW), kinds)
        self.assertIn((5, SwingKind.HIGH), kinds)
        self.assertIn((8, SwingKind.LOW), kinds)

    def test_swing_confirmation_no_repaint(self):
        candles = _structure_pattern()
        det = SwingDetector(2, 2)
        for s in det.detect(candles):
            self.assertEqual(s.confirmed_index, s.index + 2,
                             "swing must confirm exactly right-bars later")

    def test_higher_high_lower_low_trend(self):
        highs = [SwingPoint(0, _t(0), 100, SwingKind.HIGH, 2),
                 SwingPoint(4, _t(4), 102, SwingKind.HIGH, 6)]
        lows = [SwingPoint(2, _t(2), 98, SwingKind.LOW, 4),
                SwingPoint(6, _t(6), 99, SwingKind.LOW, 8)]
        trend = StructureAnalyzer._classify_trend(
            sorted(highs + lows, key=lambda s: s.index), TrendState.UNDEFINED)
        self.assertEqual(trend, TrendState.BULLISH)
        lows2 = [SwingPoint(2, _t(2), 98, SwingKind.LOW, 4),
                 SwingPoint(6, _t(6), 97, SwingKind.LOW, 8)]
        highs2 = [SwingPoint(0, _t(0), 102, SwingKind.HIGH, 2),
                  SwingPoint(4, _t(4), 100, SwingKind.HIGH, 6)]
        trend2 = StructureAnalyzer._classify_trend(
            sorted(highs2 + lows2, key=lambda s: s.index), TrendState.UNDEFINED)
        self.assertEqual(trend2, TrendState.BEARISH)


class TestStructureEvents(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.candles = _structure_pattern()
        self.state = StructureAnalyzer(self.cfg).analyze(self.candles)

    def test_bos_detected(self):
        bos = [e for e in self.state.events
               if e.kind == StructureEventKind.BOS
               and e.direction == Direction.LONG]
        self.assertTrue(bos, "bullish BOS at the displacement break expected")
        self.assertEqual(bos[0].index, 11)
        self.assertAlmostEqual(bos[0].broken_level, 101.8)

    def test_choch_or_mss_detected(self):
        counter = [e for e in self.state.events
                   if e.direction == Direction.SHORT
                   and e.kind in (StructureEventKind.CHOCH,
                                  StructureEventKind.MSS)]
        self.assertTrue(counter, "bearish CHoCH/MSS at close below HL expected")
        self.assertEqual(counter[0].index, 17)
        self.assertAlmostEqual(counter[0].broken_level, 100.0)
        self.assertEqual(self.state.trend, TrendState.BEARISH)

    def test_mss_requires_displacement(self):
        # rebuild with an enormous displacement threshold: same break must
        # degrade from MSS to plain CHoCH
        cfg2 = replace(self.cfg, displacement_atr_mult=50.0)
        state2 = StructureAnalyzer(cfg2).analyze(self.candles)
        counter = [e for e in state2.events if e.direction == Direction.SHORT
                   and e.index == 17]
        self.assertTrue(counter)
        self.assertEqual(counter[0].kind, StructureEventKind.CHOCH)

    def test_premium_discount(self):
        analyzer = StructureAnalyzer(self.cfg)
        state = self.state
        self.assertIsNotNone(state.dealing_range)
        label_hi, pos_hi = analyzer.premium_discount(state,
                                                     state.dealing_range.high - 0.01)
        label_lo, pos_lo = analyzer.premium_discount(state,
                                                     state.dealing_range.low + 0.01)
        mid = (state.dealing_range.high + state.dealing_range.low) / 2
        label_eq, _ = analyzer.premium_discount(state, mid)
        self.assertEqual(label_hi, "PREMIUM")
        self.assertEqual(label_lo, "DISCOUNT")
        self.assertEqual(label_eq, "EQUILIBRIUM")
        self.assertGreater(pos_hi, 0.9)
        self.assertLess(pos_lo, 0.1)


class TestLiquidity(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_sweep_requires_close_back_or_displacement(self):
        lvl = LiquidityLevel("L1", LiquidityKind.SWING_HIGH, 105.0, _t(0),
                             buy_side=True)
        base = _flat(0, 16, 104.0)
        # candle wicks through 105 and closes back below => valid sweep
        swept = base + [_mk(16, 104.0, 105.5, 103.8, 104.2)]
        det = LiquidityDetector(self.cfg)
        events = det.update_states([lvl], swept)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].valid)
        self.assertEqual(lvl.state, LiquidityState.SWEPT)

    def test_wick_alone_is_not_enough_when_price_stays_beyond(self):
        lvl = LiquidityLevel("L2", LiquidityKind.SWING_HIGH, 105.0, _t(0),
                             buy_side=True)
        base = _flat(0, 16, 104.0)
        # closes ABOVE the level with no displacement back => invalidated
        through = base + [_mk(16, 104.0, 105.8, 103.9, 105.6),
                          _mk(17, 105.6, 106.0, 105.3, 105.8),
                          _mk(18, 105.8, 106.2, 105.5, 106.0)]
        det = LiquidityDetector(self.cfg)
        events = det.update_states([lvl], through)
        self.assertEqual(events, [])
        self.assertEqual(lvl.state, LiquidityState.INVALIDATED)

    def test_equal_highs_cluster(self):
        swings = [SwingPoint(2, _t(2), 105.00, SwingKind.HIGH, 4),
                  SwingPoint(8, _t(8), 105.05, SwingKind.HIGH, 10),
                  SwingPoint(14, _t(14), 101.0, SwingKind.HIGH, 16)]
        candles = _flat(0, 30, 103.0)     # ATR ~1 => tol ~0.12
        det = LiquidityDetector(self.cfg)
        levels = det.detect_levels(candles, swings)
        eqh = [l for l in levels if l.kind == LiquidityKind.EQUAL_HIGHS]
        self.assertEqual(len(eqh), 1)
        self.assertAlmostEqual(eqh[0].price, 105.05)
        self.assertEqual(len(eqh[0].member_prices), 2)

    def test_previous_day_levels(self):
        candles = []
        for day in range(3):
            for k in range(288):
                i = day * 288 + k
                px = 100.0 + day * 2
                candles.append(Candle(
                    datetime(2025, 1, 6, tzinfo=UTC) + timedelta(minutes=5 * i),
                    px, px + 1 + day, px - 1 - day, px, 100, 30))
        det = LiquidityDetector(self.cfg)
        levels = det.detect_levels(candles, [])
        pdh = [l for l in levels if l.kind == LiquidityKind.PDH]
        pdl = [l for l in levels if l.kind == LiquidityKind.PDL]
        self.assertTrue(pdh and pdl)
        self.assertAlmostEqual(pdh[0].price, 104.0)   # day1: 102 +1+1
        self.assertAlmostEqual(pdl[0].price, 100.0)


class TestZones(unittest.TestCase):
    def _dbr_pattern(self) -> List[Candle]:
        out = _flat(0, 15, 100.0)                       # ATR warmup (~1.0)
        out.append(_mk(15, 100.0, 100.2, 98.6, 98.8))   # drop leg-in
        out.append(_mk(16, 98.8, 99.0, 98.3, 98.5))     # drop continues
        out.append(_mk(17, 98.5, 98.8, 98.3, 98.6))     # base 1 (small body)
        out.append(_mk(18, 98.6, 98.9, 98.4, 98.5))     # base 2 (small body)
        out.append(_mk(19, 98.5, 100.9, 98.4, 100.8))   # rally leg-out (displacement)
        out.append(_mk(20, 100.8, 101.2, 100.5, 101.0))
        return out

    def test_zone_creation_dbr(self):
        candles = self._dbr_pattern()
        zones = SupplyDemandDetector(Config(), Timeframe.M15).detect(candles)
        demand = [z for z in zones if z.kind == ZoneKind.DEMAND]
        self.assertTrue(demand, "DBR demand zone expected")
        z = demand[-1]
        self.assertEqual(z.pattern, ZonePattern.DBR)
        self.assertAlmostEqual(z.lower, 98.3)
        self.assertGreater(z.displacement_score, 0.4)
        self.assertFalse(z.invalidated)

    def test_zone_invalidation(self):
        candles = self._dbr_pattern()
        candles.append(_mk(21, 101.0, 101.1, 97.5, 97.8))  # close below distal
        zones = SupplyDemandDetector(Config(), Timeframe.M15).detect(candles)
        demand = [z for z in zones if z.kind == ZoneKind.DEMAND]
        self.assertTrue(demand)
        self.assertTrue(demand[-1].invalidated)

    def test_zone_freshness_decays_with_touches(self):
        candles = self._dbr_pattern()
        fresh = SupplyDemandDetector(Config(), Timeframe.M15).detect(candles)
        f0 = [z for z in fresh if z.kind == ZoneKind.DEMAND][-1].freshness
        for k in range(3):   # three re-entries into the zone
            candles.append(_mk(21 + k * 2, 100.5, 100.7, 98.5, 100.3))
            candles.append(_mk(22 + k * 2, 100.3, 100.9, 100.1, 100.6))
        touched = SupplyDemandDetector(Config(), Timeframe.M15).detect(candles)
        zs = [z for z in touched if z.kind == ZoneKind.DEMAND
              and abs(z.lower - 98.3) < 0.01]
        self.assertTrue(zs)
        self.assertLess(zs[-1].freshness, f0)
        self.assertGreaterEqual(zs[-1].touches, 2)


class TestFVG(unittest.TestCase):
    def test_bullish_fvg_detection(self):
        candles = _flat(0, 15, 100.0)
        candles.append(_mk(15, 100.0, 100.4, 99.8, 100.2))   # high 100.4
        candles.append(_mk(16, 100.2, 102.6, 100.1, 102.5))  # displacement
        candles.append(_mk(17, 102.5, 103.0, 101.6, 102.8))  # low 101.6 > 100.4
        gaps = FVGDetector(Config(), Timeframe.M5).detect(candles)
        bull = [g for g in gaps if g.direction == Direction.LONG]
        self.assertTrue(bull)
        g = bull[-1]
        self.assertAlmostEqual(g.lower, 100.4)
        self.assertAlmostEqual(g.upper, 101.6)
        self.assertTrue(g.from_displacement)
        self.assertEqual(g.state, FVGState.UNFILLED)

    def test_fvg_partial_and_full_mitigation(self):
        candles = _flat(0, 15, 100.0)
        candles.append(_mk(15, 100.0, 100.4, 99.8, 100.2))
        candles.append(_mk(16, 100.2, 102.6, 100.1, 102.5))
        candles.append(_mk(17, 102.5, 103.0, 101.6, 102.8))
        candles.append(_mk(18, 102.8, 103.0, 101.0, 102.0))  # partial fill
        gaps = FVGDetector(Config(), Timeframe.M5).detect(candles)
        g = [x for x in gaps if x.direction == Direction.LONG][-1]
        self.assertEqual(g.state, FVGState.PARTIAL)
        self.assertGreater(g.fill_fraction, 0.3)
        candles.append(_mk(19, 102.0, 102.2, 100.2, 100.5))  # full fill
        gaps2 = FVGDetector(Config(), Timeframe.M5).detect(candles)
        g2 = [x for x in gaps2 if x.direction == Direction.LONG][-1]
        self.assertEqual(g2.state, FVGState.MITIGATED)

    def test_bearish_fvg(self):
        candles = _flat(0, 15, 100.0)
        candles.append(_mk(15, 100.0, 100.3, 99.6, 99.8))    # low 99.6
        candles.append(_mk(16, 99.8, 99.9, 97.4, 97.5))      # displacement dn
        candles.append(_mk(17, 97.5, 98.4, 97.0, 97.3))      # high 98.4 < 99.6
        gaps = FVGDetector(Config(), Timeframe.M5).detect(candles)
        bear = [g for g in gaps if g.direction == Direction.SHORT]
        self.assertTrue(bear)
        self.assertAlmostEqual(bear[-1].upper, 99.6)
        self.assertAlmostEqual(bear[-1].lower, 98.4)


class TestOrderBlocks(unittest.TestCase):
    @staticmethod
    def _shifted(pattern: List[Candle], offset: int) -> List[Candle]:
        return [Candle(_t(offset + k), c.open, c.high, c.low, c.close,
                       c.volume, c.spread) for k, c in enumerate(pattern)]

    def test_ob_from_structure_break(self):
        # prepend warmup so the displacement candle sits past the ATR window
        candles = _flat(0, 12, 100.0) + self._shifted(_structure_pattern(), 12)
        cfg = Config()
        state = StructureAnalyzer(cfg).analyze(candles)
        obs = OrderBlockDetector(cfg, Timeframe.M5).detect(candles, state)
        bull = [b for b in obs if b.direction == Direction.LONG]
        self.assertTrue(bull, "bullish OB before the BOS displacement expected")
        self.assertEqual(bull[0].created_index, 20)  # last bearish before @23
        self.assertEqual(bull[0].linked_structure, StructureEventKind.BOS)

    def test_plain_opposite_candle_is_not_ob(self):
        candles = _flat(0, 30, 100.0)    # no displacement anywhere
        cfg = Config()
        state = StructureAnalyzer(cfg).analyze(candles)
        obs = OrderBlockDetector(cfg, Timeframe.M5).detect(candles, state)
        self.assertEqual(obs, [])


class TestRegime(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def _trend_up(self) -> List[Candle]:
        """Clean rising zigzag: 6 candles up, 2-candle pullback -> HH+HL."""
        out: List[Candle] = []
        px = 100.0
        i = 0
        while len(out) < 120:
            for _ in range(6):
                o, c = px, px + 0.5
                out.append(_mk(i, o, c + 0.2, o - 0.2, c))
                px = c
                i += 1
            for _ in range(2):
                o, c = px, px - 0.3
                out.append(_mk(i, o, o + 0.15, c - 0.2, c))
                px = c
                i += 1
        return out[:120]

    def test_bull_trend_regime(self):
        candles = self._trend_up()
        state = StructureAnalyzer(self.cfg).analyze(candles)
        r = MarketRegimeDetector(self.cfg).classify(candles, state, 30.0)
        self.assertIn(r.regime, (Regime.STRONG_BULL, Regime.WEAK_BULL,
                                 Regime.EXPANSION))

    def test_abnormal_spread_regime(self):
        candles = self._trend_up()
        state = StructureAnalyzer(self.cfg).analyze(candles)
        r = MarketRegimeDetector(self.cfg).classify(candles, state,
                                                    spread_points=150.0)
        self.assertEqual(r.regime, Regime.ABNORMAL_SPREAD)
        self.assertEqual(StrategyEngine.MODELS_BY_REGIME[r.regime], ())

    def test_news_regime_blocks_models(self):
        candles = self._trend_up()
        state = StructureAnalyzer(self.cfg).analyze(candles)
        r = MarketRegimeDetector(self.cfg).classify(candles, state, 30.0,
                                                    news_active=True)
        self.assertEqual(r.regime, Regime.NEWS_VOLATILITY)
        self.assertEqual(StrategyEngine.MODELS_BY_REGIME[r.regime], ())

    def test_insufficient_history_is_unsafe(self):
        candles = _flat(0, 10, 100.0)
        state = StructureAnalyzer(self.cfg).analyze(candles)
        r = MarketRegimeDetector(self.cfg).classify(candles, state, 30.0)
        self.assertEqual(r.regime, Regime.UNSAFE)


class TestTimeframeSelection(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.counts = {tf: 500 for tf in TF_ORDER}

    def test_high_volatility_selects_htf_combo(self):
        plan = TimeframeSelector(self.cfg).select(
            self.counts, atr_percentile=0.95, spread_points=30,
            atr_points=300, session=SessionName.LONDON,
            regime=Regime.EXPANSION, m1_efficiency=0.5)
        self.assertEqual(plan.bias_tf, Timeframe.D1)
        self.assertEqual(plan.entry_tf, Timeframe.M15)
        self.assertIn("high volatility", plan.reason)

    def test_noisy_m1_is_excluded(self):
        plan = TimeframeSelector(self.cfg).select(
            self.counts, atr_percentile=0.5, spread_points=30,
            atr_points=300, session=SessionName.LONDON,
            regime=Regime.STRONG_BULL, m1_efficiency=0.05)
        self.assertNotEqual(plan.entry_tf, Timeframe.M1)
        self.assertIn("M1 excluded", plan.reason)

    def test_clean_m1_precision_combo(self):
        plan = TimeframeSelector(self.cfg).select(
            self.counts, atr_percentile=0.5, spread_points=20,
            atr_points=400, session=SessionName.OVERLAP,
            regime=Regime.STRONG_BEAR, m1_efficiency=0.5)
        self.assertEqual(plan.entry_tf, Timeframe.M1)
        self.assertEqual(plan.bias_tf, Timeframe.H1)

    def test_off_hours_conservative(self):
        plan = TimeframeSelector(self.cfg).select(
            self.counts, atr_percentile=0.5, spread_points=30,
            atr_points=300, session=SessionName.OFF_HOURS,
            regime=Regime.RANGE, m1_efficiency=0.5)
        self.assertEqual(plan.bias_tf, Timeframe.H4)
        self.assertEqual(plan.entry_tf, Timeframe.M15)


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.scorer = SetupScorer(self.cfg)
        self.zone = Zone("z", ZoneKind.SUPPLY, ZonePattern.RBD, 102, 101,
                         Timeframe.H1, _t(0), 0, freshness=1.0,
                         displacement_score=1.0, caused_bos=True,
                         fvg_overlap=True, htf_aligned=True)
        self.sweep = SweepEvent(
            LiquidityLevel("l", LiquidityKind.EQUAL_HIGHS, 102, _t(0), True),
            10, _t(10), 102.5, closed_back=True, displaced_away=True)
        self.event = StructureEvent(StructureEventKind.MSS, Direction.SHORT,
                                    12, _t(12), 101.0, None, displacement=True)

    def test_full_confluence_scores_a_plus(self):
        b = self.scorer.score(
            Direction.SHORT, TrendState.BEARISH, self.zone, self.sweep,
            self.event, True,
            FairValueGap("f", Direction.SHORT, 102, 101.5, Timeframe.M15,
                         _t(0), 0),
            OrderBlock("o", Direction.SHORT, 102.2, 101.8, Timeframe.M15,
                       _t(0), 0),
            "PREMIUM", SessionName.LONDON, news_blocked=False,
            news_protection_complete=True, rr_tp1=3.2,
            target_is_liquidity=True)
        self.assertGreaterEqual(b.total, 90)
        self.assertEqual(SetupGrade.from_score(b.total), SetupGrade.A_PLUS)

    def test_countertrend_loses_alignment_points(self):
        b = self.scorer.score(
            Direction.LONG, TrendState.BEARISH, None, None, self.event,
            False, None, None, "EQUILIBRIUM", SessionName.ASIA,
            news_blocked=False, news_protection_complete=False,
            rr_tp1=1.2, target_is_liquidity=False)
        self.assertEqual(b.htf_alignment, 0.0)
        self.assertLess(b.total, 70)
        self.assertEqual(SetupGrade.from_score(b.total), SetupGrade.NO_TRADE)

    def test_breakdown_totals_are_consistent(self):
        b = ScoreBreakdown(htf_alignment=15, zone_quality=10,
                           liquidity_sweep=15, structure_confirmation=12,
                           displacement=10, confluence=5,
                           premium_discount=5, session_quality=5,
                           news_safety=3, target_quality=4)
        self.assertAlmostEqual(b.total, 84.0)
        self.assertEqual(SetupGrade.from_score(b.total), SetupGrade.A)


class TestPositionSizing(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.spec = default_xauusd_spec()
        self.sizer = PositionSizer(self.cfg)

    def test_size_respects_intended_risk(self):
        res = self.sizer.size(self.spec, equity=10_000, risk_fraction=0.01,
                              entry=2400.0, stop=2395.0)
        self.assertFalse(res.rejected)
        self.assertGreater(res.volume, 0)
        self.assertLessEqual(res.risk_money, 100.0 + 1e-6)
        self.assertEqual(res.volume,
                         self.spec.round_volume_down(res.volume))

    def test_volume_rounds_down_never_up(self):
        res = self.sizer.size(self.spec, 10_000, 0.01, 2400.0, 2395.0)
        # one step more volume would exceed intended risk
        loss_per_lot = res.risk_money / res.volume
        self.assertGreater((res.volume + self.spec.volume_step) * loss_per_lot,
                           res.intended_risk_money)

    def test_minimum_lot_rejection(self):
        res = self.sizer.size(self.spec, equity=500, risk_fraction=0.005,
                              entry=2400.0, stop=2395.0)
        self.assertTrue(res.rejected)
        self.assertIn("minimum lot", res.reason)

    def test_hard_cap_5_percent(self):
        res = self.sizer.size(self.spec, 10_000, 0.20, 2400.0, 2395.0)
        self.assertLessEqual(res.risk_money, 10_000 * 0.05 + 1e-6)

    def test_grade_risk_defaults(self):
        f = self.sizer.risk_fraction_for
        self.assertAlmostEqual(f(SetupGrade.A_PLUS, 95), 0.02)
        self.assertGreaterEqual(f(SetupGrade.A, 85), 0.01)
        self.assertLessEqual(f(SetupGrade.A, 89), 0.015 + 1e-9)
        self.assertGreaterEqual(f(SetupGrade.B, 70), 0.005)
        self.assertEqual(f(SetupGrade.NO_TRADE, 60), 0.0)

    def test_aggressive_mode_is_explicit_and_capped(self):
        self.assertAlmostEqual(
            self.sizer.risk_fraction_for(SetupGrade.A_PLUS, 95), 0.02)
        cfg2 = replace(self.cfg, aggressive_mode=True)
        sizer2 = PositionSizer(cfg2)
        self.assertAlmostEqual(
            sizer2.risk_fraction_for(SetupGrade.A_PLUS, 95), 0.05)
        cfg3 = replace(self.cfg, aggressive_mode=True,
                       aggressive_risk_a_plus=0.99)
        self.assertLessEqual(
            PositionSizer(cfg3).risk_fraction_for(SetupGrade.A_PLUS, 95),
            cfg3.hard_max_risk_per_trade)


class TestRiskLocks(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.db = DatabaseManager(":memory:")
        self.rm = RiskManager(self.cfg, self.db)
        self.t0 = datetime(2025, 1, 7, 9, 0, tzinfo=UTC)
        self.rm.roll(self.t0, 10_000.0)

    def test_daily_loss_lock(self):
        self.rm.register_close(-600.0)      # -6% of start-of-day equity
        self.assertEqual(self.rm.lock_reason(self.t0, 9_400.0),
                         LockReason.DAILY_LOSS)

    def test_daily_loss_includes_unrealised(self):
        self.rm.register_close(-300.0)
        self.assertEqual(self.rm.lock_reason(self.t0, 9_700.0,
                                             unrealised=-250.0),
                         LockReason.DAILY_LOSS)

    def test_daily_target_lock(self):
        self.rm.register_close(+320.0)      # +3.2%
        self.assertEqual(self.rm.lock_reason(self.t0, 10_320.0),
                         LockReason.DAILY_TARGET)

    def test_soft_throttle_reduces_risk(self):
        self.rm.register_close(+250.0)      # +2.5% => soft stop
        self.assertTrue(self.rm.soft_throttle_active())
        self.assertAlmostEqual(self.rm.adjusted_risk(0.01),
                               0.01 * self.cfg.soft_stop_risk_factor)

    def test_soft_throttle_never_increases_risk(self):
        self.assertLessEqual(self.rm.adjusted_risk(0.01), 0.01)

    def test_weekly_loss_lock(self):
        self.rm.roll(self.t0 + timedelta(days=1), 8_900.0)   # -11% on week
        self.assertEqual(self.rm.lock_reason(self.t0 + timedelta(days=1),
                                             8_900.0),
                         LockReason.WEEKLY_LOSS)

    def test_consecutive_loss_lock_and_reset(self):
        for _ in range(3):
            self.rm.register_close(-10.0)
        self.assertEqual(self.rm.lock_reason(self.t0, 9_970.0),
                         LockReason.CONSECUTIVE_LOSSES)
        self.rm.consecutive_losses = 2
        self.rm.register_close(+50.0)
        self.assertEqual(self.rm.consecutive_losses, 0)

    def test_max_trades_per_day(self):
        for _ in range(self.cfg.max_trades_per_day):
            self.rm.register_open(self.t0, SessionName.LONDON)
        self.assertEqual(self.rm.lock_reason(self.t0, 10_000.0),
                         LockReason.MAX_TRADES_DAY)

    def test_max_trades_per_session(self):
        for _ in range(self.cfg.max_trades_per_session):
            self.rm.register_open(self.t0, SessionName.LONDON)
        self.assertEqual(self.rm.lock_reason(self.t0, 10_000.0,
                                             session=SessionName.LONDON),
                         LockReason.MAX_TRADES_SESSION)

    def test_locks_clear_on_new_day(self):
        self.rm.register_close(-600.0)
        next_day = self.t0 + timedelta(days=1)
        self.rm.roll(next_day, 9_400.0)
        self.assertEqual(self.rm.lock_reason(next_day, 9_400.0),
                         LockReason.NONE)

    def test_restart_recovery(self):
        self.rm.register_close(-600.0)
        rm2 = RiskManager(self.cfg, self.db)     # fresh instance, same DB
        self.assertIsNotNone(rm2.day)
        self.assertEqual(rm2.day.day, self.rm.day.day)
        self.assertEqual(rm2.lock_reason(self.t0, 9_400.0),
                         LockReason.DAILY_LOSS)

    def test_combined_open_risk_cap(self):
        ok, why = self.rm.can_open(self.t0, 10_000.0, 0,
                                   open_risk_money=400.0,
                                   new_risk_money=200.0)
        self.assertFalse(ok)
        self.assertIn("combined open risk", why)


class TestTradeHelpers(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.spec = default_xauusd_spec()

    def _trade(self, direction=Direction.LONG, entry=100.0, stop=95.0,
               vol=1.0) -> Trade:
        plan = TimeframePlan(Timeframe.H1, Timeframe.M15, Timeframe.M15,
                             Timeframe.M5, Timeframe.M15, "test")
        setup = Setup("s1", SetupModel.TREND_CONTINUATION, direction,
                      _t(0), entry, entry, stop, entry + 10 * direction.sign,
                      entry + 15 * direction.sign, None,
                      EntryMode.MARKET_ON_CONFIRM, 85, SetupGrade.A,
                      ScoreBreakdown(), plan, Regime.STRONG_BULL,
                      SessionName.LONDON, TrendState.BULLISH, atr=1.0)
        return Trade("t1", setup, TradeStatus.OPEN, vol, vol, 0.01,
                     abs(entry - stop) * 100 * vol, entry_price=entry,
                     entry_time=_t(0), stop_price=stop, initial_stop=stop,
                     tp1=setup.tp1, tp2=setup.tp2)

    def _rising_tfa(self, engine: StrategyEngine, last_close: float) -> TFAnalysis:
        candles = []
        px = 99.0
        step = (last_close - 99.0) / 39
        for i in range(40):
            o = px
            c = px + step
            candles.append(_mk(i, o, max(o, c) + 0.2, min(o, c) - 0.2, c))
            px = c
        return engine.build_tf_analysis(Timeframe.M15, candles)

    def test_breakeven_calculation(self):
        cfg = replace(self.cfg, breakeven_needs_structure=False)
        tmgr = TradeManager(cfg)
        engine = StrategyEngine(cfg)
        tr = self._trade()                      # long 100, stop 95, R=5
        tfa = self._rising_tfa(engine, 105.5)   # +1.1R
        actions = tmgr.manage(tr, tfa, _t(40), self.spec)
        moves = [a for a in actions if a.kind == "MOVE_STOP"
                 and a.reason == ExitReason.BREAK_EVEN]
        self.assertTrue(moves, "break-even move expected at +1.1R")
        self.assertGreaterEqual(moves[0].price, tr.entry_price)
        self.assertGreater(moves[0].price, tr.initial_stop)

    def test_no_premature_breakeven_below_1r(self):
        cfg = replace(self.cfg, breakeven_needs_structure=False)
        tmgr = TradeManager(cfg)
        engine = StrategyEngine(cfg)
        tr = self._trade()
        tfa = self._rising_tfa(engine, 103.0)   # only +0.6R
        actions = tmgr.manage(tr, tfa, _t(40), self.spec)
        self.assertFalse([a for a in actions
                          if a.reason == ExitReason.BREAK_EVEN])

    def test_partial_close_calculation(self):
        cfg = replace(self.cfg, breakeven_needs_structure=False)
        tmgr = TradeManager(cfg)
        engine = StrategyEngine(cfg)
        tr = self._trade(vol=1.0)
        tfa = self._rising_tfa(engine, 109.5)   # +1.9R > partial_r 1.7
        actions = tmgr.manage(tr, tfa, _t(40), self.spec)
        partials = [a for a in actions if a.kind == "PARTIAL_CLOSE"]
        self.assertTrue(partials)
        self.assertAlmostEqual(partials[0].volume, 0.40, places=2)
        self.assertLess(partials[0].volume, tr.volume)

    def test_stop_never_widens(self):
        tr = self._trade()                      # long, stop 95
        self.assertFalse(TradeManager._tightens(tr, 94.0))
        self.assertTrue(TradeManager._tightens(tr, 97.0))
        execu = ExecutionManager(self.cfg, DatabaseManager(":memory:"))
        execu.paper_apply_action(
            ManagementAction("MOVE_STOP", tr, price=90.0), self.spec, _t(1))
        self.assertEqual(tr.stop_price, 95.0, "widening must be ignored")
        tr_short = self._trade(direction=Direction.SHORT, entry=100.0,
                               stop=105.0)
        self.assertFalse(TradeManager._tightens(tr_short, 106.0))
        self.assertTrue(TradeManager._tightens(tr_short, 103.0))

    def test_time_exit(self):
        tmgr = TradeManager(self.cfg)
        engine = StrategyEngine(self.cfg)
        tr = self._trade()
        tfa = self._rising_tfa(engine, 100.5)
        late = tr.entry_time + timedelta(hours=self.cfg.time_exit_hours + 1)
        actions = tmgr.manage(tr, tfa, late, self.spec)
        self.assertTrue([a for a in actions
                         if a.reason == ExitReason.TIME_EXIT])

    def test_weekend_exit(self):
        tmgr = TradeManager(self.cfg)
        engine = StrategyEngine(self.cfg)
        tr = self._trade()
        tfa = self._rising_tfa(engine, 101.0)
        actions = tmgr.manage(tr, tfa, _t(10), self.spec, weekend_flat=True)
        self.assertTrue([a for a in actions
                         if a.reason == ExitReason.WEEKEND_EXIT])

    def test_paper_stop_fill_conservative(self):
        """SL and TP both inside one candle => stop fills first."""
        execu = ExecutionManager(self.cfg, DatabaseManager(":memory:"))
        tr = self._trade()                      # long 100 stop 95 tp1 110
        execu.open_trades.append(tr)
        wide = _mk(50, 100.0, 111.0, 94.0, 108.0)   # touches both
        closed = execu.paper_update_open(wide, self.spec, _t(51))
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].exit_reason, ExitReason.STOP_LOSS)
        self.assertLess(closed[0].profit, 0)

    def test_duplicate_prevention(self):
        execu = ExecutionManager(self.cfg, DatabaseManager(":memory:"))
        tr = self._trade()
        execu.open_trades.append(tr)
        dup = tr.setup
        self.assertTrue(execu.duplicate(dup))
        opposite = replace(dup, direction=Direction.SHORT,
                           setup_id="s2")
        self.assertTrue(execu.conflicting(opposite))


class TestSessionsAndNews(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.sm = SessionManager(self.cfg)

    def test_session_classification(self):
        d = datetime(2025, 1, 7, tzinfo=UTC)     # Tuesday
        self.assertEqual(self.sm.session_at(d.replace(hour=3)), SessionName.ASIA)
        self.assertEqual(self.sm.session_at(d.replace(hour=8)), SessionName.LONDON)
        self.assertEqual(self.sm.session_at(d.replace(hour=13)), SessionName.OVERLAP)
        self.assertEqual(self.sm.session_at(d.replace(hour=18)), SessionName.NEW_YORK)
        self.assertEqual(self.sm.session_at(d.replace(hour=22)), SessionName.OFF_HOURS)

    def test_entry_window_rules(self):
        sat = datetime(2025, 1, 11, 10, 0, tzinfo=UTC)
        self.assertFalse(self.sm.entry_window_check(sat)[0])
        fri_late = datetime(2025, 1, 10, 16, 0, tzinfo=UTC)
        ok, why = self.sm.entry_window_check(fri_late)
        self.assertFalse(ok)
        self.assertIn("Friday", why)
        mon_early = datetime(2025, 1, 6, 1, 0, tzinfo=UTC)
        self.assertFalse(self.sm.entry_window_check(mon_early)[0])
        rollover = datetime(2025, 1, 7, 21, 30, tzinfo=UTC)
        ok2, why2 = self.sm.entry_window_check(rollover)
        self.assertFalse(ok2)
        self.assertIn("rollover", why2)
        good = datetime(2025, 1, 7, 9, 0, tzinfo=UTC)
        self.assertTrue(self.sm.entry_window_check(good)[0])

    def test_dst_conversion_madrid(self):
        winter = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
        summer = datetime(2025, 7, 15, 12, 0, tzinfo=UTC)
        self.assertEqual(self.sm.to_user_time(winter).hour, 13)  # CET +1
        self.assertEqual(self.sm.to_user_time(summer).hour, 14)  # CEST +2

    def test_session_range_tracking(self):
        candles = [
            Candle(datetime(2025, 1, 7, 2, 0, tzinfo=UTC), 100, 105, 99, 101, 1, 30),
            Candle(datetime(2025, 1, 7, 4, 0, tzinfo=UTC), 101, 103, 98, 100, 1, 30),
        ]
        self.sm.rebuild_from(candles)
        rng = self.sm.asian_range(date(2025, 1, 7))
        self.assertEqual(rng, (98.0, 105.0))

    def test_manual_news_blackout(self):
        cfg = replace(self.cfg, manual_blackouts_utc=(
            "2025-03-10T14:00/2025-03-10T15:00",))
        nf = NewsFilter(cfg)
        blocked, why = nf.blackout(datetime(2025, 3, 10, 14, 30, tzinfo=UTC))
        self.assertTrue(blocked)
        free, _ = nf.blackout(datetime(2025, 3, 10, 16, 30, tzinfo=UTC))
        self.assertFalse(free)

    def test_news_event_window_margins(self):
        nf = NewsFilter(self.cfg)
        ev_time = datetime(2025, 3, 12, 13, 30, tzinfo=UTC)
        nf._cache = [NewsEvent(ev_time, "CPI", "USD", "HIGH")]
        nf._cache_time = ev_time
        nf.api = ApiNewsProvider("k", "http://example.invalid")
        nf.api._last_ok = True
        blocked_before, _ = nf.blackout(ev_time - timedelta(minutes=25))
        blocked_after, _ = nf.blackout(ev_time + timedelta(minutes=25))
        free_far, _ = nf.blackout(ev_time - timedelta(minutes=60))
        self.assertTrue(blocked_before)
        self.assertTrue(blocked_after)
        self.assertFalse(free_far)

    def test_no_key_means_incomplete_protection_not_fake_events(self):
        nf = NewsFilter(self.cfg)                # no NEWS_API_KEY
        self.assertFalse(nf.protection_complete())
        blocked, _ = nf.blackout(datetime(2025, 3, 12, 13, 25, tzinfo=UTC))
        self.assertFalse(blocked, "no provider must not invent blackouts")


class TestResampleNoLookahead(unittest.TestCase):
    def test_aggregation_correctness(self):
        candles = [
            _mk(0, 100, 101, 99, 100.5),   # 08:00
            _mk(1, 100.5, 102, 100, 101),  # 08:05
            _mk(2, 101, 101.5, 100.2, 100.4),  # 08:10
            _mk(3, 100.4, 103, 100.3, 102.9),  # 08:15
            _mk(4, 102.9, 104, 102, 103),  # 08:20
            _mk(5, 103, 103.5, 102.5, 103.2),  # 08:25
        ]
        m15 = resample(candles, Timeframe.M15)
        self.assertEqual(len(m15), 2)
        self.assertEqual(m15[0].open, 100)
        self.assertEqual(m15[0].high, 102)
        self.assertEqual(m15[0].low, 99)
        self.assertEqual(m15[0].close, 100.4)
        self.assertEqual(m15[1].close, 103.2)

    def test_incomplete_bucket_is_withheld(self):
        candles = [_mk(i, 100, 101, 99, 100) for i in range(5)]  # 08:00-08:20
        m15 = resample(candles, Timeframe.M15)
        self.assertEqual(len(m15), 1, "the 08:15 bucket is incomplete "
                         "(only 2 of 3 candles) and must not be emitted")

    def test_atr_series_no_future_dependency(self):
        candles = [_mk(i, 100 + i, 101 + i, 99 + i, 100.5 + i)
                   for i in range(40)]
        full = atr_series(candles, 14)
        partial = atr_series(candles[:30], 14)
        for k in range(30):
            self.assertAlmostEqual(full[k], partial[k], places=10,
                                   msg="ATR at index k must not depend on "
                                       "later candles")


class TestSafeguards(unittest.TestCase):
    def test_config_validator_defaults_pass(self):
        cfg = Config()
        v = ConfigValidator(cfg)
        self.assertTrue(v.validate(), v.report())
        self.assertEqual(cfg.mode, Mode.PAPER)
        self.assertFalse(cfg.live_trading_enabled)
        self.assertFalse(cfg.aggressive_mode)

    def test_live_mode_requires_explicit_enable(self):
        cfg = replace(Config(), mode=Mode.LIVE)
        v = ConfigValidator(cfg)
        self.assertFalse(v.validate())
        self.assertTrue(any("LIVE_TRADING_ENABLED" in e for e in v.errors))

    def test_hard_cap_cannot_be_raised(self):
        cfg = replace(Config(), hard_max_risk_per_trade=0.10)
        v = ConfigValidator(cfg)
        self.assertFalse(v.validate())

    def test_aggressive_risk_capped(self):
        cfg = replace(Config(), aggressive_risk_a_plus=0.08)
        v = ConfigValidator(cfg)
        self.assertFalse(v.validate())

    def test_live_gate_blocks_without_verification(self):
        cfg = replace(Config(), mode=Mode.LIVE, live_trading_enabled=True,
                      live_account_number=123, live_server="X-Live")
        db = DatabaseManager(":memory:")
        gate = LiveGate(cfg, db, cli_flag=True)
        ok, failures = gate.check()
        self.assertFalse(ok)
        self.assertTrue(any("backtest_verified" in f for f in failures))
        self.assertTrue(any("no recorded backtest" in f for f in failures))

    def test_live_gate_blocks_without_cli_flag(self):
        cfg = replace(Config(), mode=Mode.LIVE, live_trading_enabled=True,
                      live_account_number=123, live_server="X-Live",
                      backtest_verified=True, paper_verified=True,
                      demo_verified=True)
        gate = LiveGate(cfg, DatabaseManager(":memory:"), cli_flag=False)
        ok, failures = gate.check()
        self.assertFalse(ok)
        self.assertTrue(any("--i-understand-live-risk" in f for f in failures))

    def test_kill_switch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ks = str(Path(td) / "KILL_SWITCH")
            cfg = replace(Config(), kill_switch_file=ks)
            hm = HealthMonitor(cfg, DatabaseManager(":memory:"))
            self.assertFalse(hm.kill_switch_active())
            Path(ks).write_text("stop")
            self.assertTrue(hm.kill_switch_active())
            self.assertEqual(hm.check(None, None, Timeframe.M5),
                             LockReason.KILL_SWITCH)

    def test_symbol_spec_volume_rounding(self):
        spec = default_xauusd_spec()
        self.assertEqual(spec.round_volume_down(0.379), 0.37)
        self.assertEqual(spec.round_volume_down(0.005), 0.0)
        self.assertEqual(spec.round_volume_down(500.0), spec.volume_max)

    def test_secrets_not_in_public_config(self):
        cfg = Config()
        cfg.mt5_password = "supersecret"
        cfg.telegram_token = "tok"
        public = json.dumps(cfg.public_dict())
        self.assertNotIn("supersecret", public)
        self.assertNotIn("tok\"", public)
        self.assertNotIn("mt5_password", public)


class TestBacktestEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = Config()
        cfg.mode = Mode.BACKTEST
        gen = SyntheticDataGenerator(seed=11)
        cls.candles = gen.generate(days=14)
        cls.db = DatabaseManager(":memory:")
        engine = BacktestEngine(cfg, cls.db, quiet=True)
        cls.result = engine.run(cls.candles, data_desc="unit-test",
                                synthetic=True)
        cls.cfg = cfg

    def test_backtest_runs_and_reports(self):
        r = self.result
        self.assertGreater(len(r.equity_curve), 1000)
        self.assertIn("net_return", r.metrics)
        self.assertIn("max_drawdown", r.metrics)
        self.assertTrue(r.assumptions)

    def test_equity_reconciles_with_trades(self):
        r = self.result
        closed = [t for t in r.trades if t.status == TradeStatus.CLOSED]
        expected = self.cfg.backtest_initial_equity + sum(t.profit
                                                          for t in closed)
        self.assertAlmostEqual(r.equity_curve[-1][1], expected, places=2)

    def test_all_closed_trades_have_stops_and_journal(self):
        for t in self.result.trades:
            if t.status == TradeStatus.CLOSED and t.entry_time:
                self.assertNotEqual(t.initial_stop, 0.0)
                self.assertGreater(t.risk_money, 0.0)
        rows = self.db.query("SELECT COUNT(*) FROM trades")
        closed_n = sum(1 for t in self.result.trades
                       if t.status == TradeStatus.CLOSED and t.entry_time)
        self.assertGreaterEqual(rows[0][0], closed_n)

    def test_risk_never_exceeds_hard_cap(self):
        for t in self.result.trades:
            if t.risk_fraction > 0:
                self.assertLessEqual(t.risk_fraction,
                                     self.cfg.hard_max_risk_per_trade + 1e-9)

    def test_rejections_are_journalled(self):
        rows = self.db.query("SELECT COUNT(*) FROM rejections")
        self.assertEqual(rows[0][0] > 0, self.result.rejections > 0)

    def test_monte_carlo_on_results(self):
        mc = MonteCarloAnalyzer(runs=200).analyze(
            self.result.trades, self.cfg.backtest_initial_equity)
        closed = [t for t in self.result.trades
                  if t.status == TradeStatus.CLOSED]
        if len(closed) >= 10:
            self.assertEqual(mc["runs"], 200)
            self.assertLessEqual(mc["final_equity_p5"], mc["final_equity_p95"])
        else:
            self.assertEqual(mc["runs"], 0)


def run_tests(verbose: bool = True) -> int:
    """Run the built-in suite; returns a process exit code."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (TestSwings, TestStructureEvents, TestLiquidity, TestZones,
                TestFVG, TestOrderBlocks, TestRegime, TestTimeframeSelection,
                TestScoring, TestPositionSizing, TestRiskLocks,
                TestTradeHelpers, TestSessionsAndNews,
                TestResampleNoLookahead, TestSafeguards, TestBacktestEngine):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


# ===========================================================================
# SECTION 34 — MAIN / CLI
# ===========================================================================

def _print_backtest_report(cfg: Config, db: DatabaseManager,
                           result: BacktestResult, synthetic: bool,
                           run_stress: bool, candles: List[Candle],
                           monte_carlo: bool) -> None:
    rg = ReportGenerator(db)
    print()
    print(rg.performance_report(result.metrics,
                                f"BACKTEST {result.start:%Y-%m-%d} .. "
                                f"{result.end:%Y-%m-%d}"))
    print("\n-- assumptions:")
    for a in result.assumptions:
        print(f"   * {a}")
    print("\n-- self-review findings:")
    print(rg.review_findings(result.trades))
    if synthetic:
        print("\n!! SYNTHETIC DATA: this run verifies engine mechanics only. "
              "It says NOTHING about real-market profitability.")
    if monte_carlo:
        mc = MonteCarloAnalyzer(runs=cfg.monte_carlo_runs).analyze(
            result.trades, cfg.backtest_initial_equity)
        print("\n-- monte carlo (trade-order reshuffle):")
        for k, v in mc.items():
            print(f"   {k}: {v}")
    if run_stress:
        stress_cfg = replace(cfg,
                             backtest_spread_points=cfg.backtest_spread_points * 1.5,
                             slippage_buffer_points=cfg.slippage_buffer_points * 2.0,
                             commission_per_lot=cfg.commission_per_lot * 1.5)
        stress = BacktestEngine(stress_cfg, DatabaseManager(":memory:"),
                                quiet=True).run(candles, "stress",
                                                synthetic=synthetic)
        print("\n-- stressed costs pass (1.5x spread, 2x slippage, "
              "1.5x commission):")
        print(f"   trades={stress.metrics.get('trades')} "
              f"net={stress.metrics.get('net_profit', 0.0):.2f} "
              f"PF={stress.metrics.get('profit_factor', 0.0):.2f} "
              f"expectancy={stress.metrics.get('expectancy_r', 0.0):+.3f}R "
              f"maxDD={stress.metrics.get('max_drawdown', 0.0):.2%}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog=BOT_NAME,
        description="Adaptive XAUUSD trading bot (backtest/paper/demo/live). "
                    "Default mode: PAPER. Live trading is gated and "
                    "disabled by default.")
    parser.add_argument("--mode", choices=[m.value for m in Mode],
                        default=None, help="override configured mode")
    parser.add_argument("--test", action="store_true",
                        help="run the built-in test suite and exit")
    parser.add_argument("--data", default="",
                        help="CSV candle file for BACKTEST "
                             "(time,open,high,low,close[,volume[,spread]])")
    parser.add_argument("--synthetic", action="store_true",
                        help="BACKTEST on synthetic data (mechanics check)")
    parser.add_argument("--days", type=int, default=30,
                        help="synthetic data length in days (default 30)")
    parser.add_argument("--seed", type=int, default=42,
                        help="synthetic data RNG seed")
    parser.add_argument("--walk-forward", action="store_true",
                        help="run walk-forward analysis after the backtest")
    parser.add_argument("--no-monte-carlo", action="store_true",
                        help="skip Monte Carlo analysis in BACKTEST")
    parser.add_argument("--no-stress", action="store_true",
                        help="skip the stressed-costs pass in BACKTEST")
    parser.add_argument("--alert-only", action="store_true",
                        help="MODE D: analyse and alert, never execute")
    parser.add_argument("--db", default="",
                        help="override SQLite database path")
    parser.add_argument("--report-day", default="",
                        help="print the daily report for YYYY-MM-DD and exit")
    parser.add_argument("--emergency-close", action="store_true",
                        help="close all bot positions at MT5 and exit")
    parser.add_argument("--i-understand-live-risk", action="store_true",
                        help="required (with config flags) for LIVE mode")
    args = parser.parse_args(argv)

    cfg = Config().load_env()
    if args.mode:
        cfg.mode = Mode(args.mode)
    if args.db:
        cfg.db_path = args.db
    if args.alert_only:
        cfg.alert_only = True
    LoggerManager.setup(cfg)

    if args.test:
        return run_tests()

    validator = ConfigValidator(cfg)
    if not validator.validate():
        print(validator.report())
        return 2
    if validator.warnings:
        print(validator.report())

    if args.emergency_close:
        return run_emergency_close(cfg)

    if args.report_day:
        db = DatabaseManager(cfg.db_path)
        print(ReportGenerator(db).daily_report(args.report_day))
        db.close()
        return 0

    if cfg.mode == Mode.BACKTEST:
        synthetic = args.synthetic or not args.data
        if args.data:
            candles = parse_csv_candles(args.data)
            desc = args.data
            if not candles:
                print(f"could not load candles from {args.data}")
                return 2
        else:
            if not args.synthetic:
                print("no --data given: falling back to --synthetic "
                      "(mechanics verification only)")
            candles = SyntheticDataGenerator(seed=args.seed).generate(
                days=args.days)
            desc = f"synthetic(seed={args.seed},days={args.days})"
        log.info("BACKTEST on %s: %d candles %s .. %s", desc, len(candles),
                 candles[0].time, candles[-1].time)
        db = DatabaseManager(cfg.db_path)
        db.record_config(cfg, "backtest run")
        engine = BacktestEngine(cfg, db)
        try:
            result = engine.run(candles, data_desc=desc, synthetic=synthetic)
        except ValueError as exc:
            print(f"backtest failed: {exc}")
            return 2
        _print_backtest_report(cfg, db, result, synthetic,
                               run_stress=not args.no_stress,
                               candles=candles,
                               monte_carlo=not args.no_monte_carlo)
        if args.walk_forward:
            print("\n-- walk-forward analysis (anchored folds):")
            wf = WalkForwardEngine(cfg, db).run(candles, synthetic=synthetic)
            for fold in wf["folds"]:
                print(f"   fold {fold['fold']}: chosen={fold['chosen']} "
                      f"train={fold['train_expectancy_r']:+.3f}R "
                      f"validate={fold['validate_expectancy_r']:+.3f}R "
                      f"({fold['validate_trades']} trades) "
                      f"OOS={fold['oos_expectancy_r']:+.3f}R "
                      f"({fold['oos_trades']} trades, "
                      f"PF {fold['oos_profit_factor']:.2f})")
                for row in fold["sensitivity"]:
                    print(f"      {row['params']} -> "
                          f"n={row['trades']} exp={row['expectancy_r']:+.3f}R "
                          f"PF={row['profit_factor']:.2f}")
            print(f"   stable across folds: {wf['stable']}")
            if wf.get("warning"):
                print(f"   warning: {wf['warning']}")
        db.close()
        return 0

    # PAPER / DEMO / LIVE
    controller = BotController(cfg, cli_live_flag=args.i_understand_live_risk)
    return controller.start()


if __name__ == "__main__":
    sys.exit(main())
