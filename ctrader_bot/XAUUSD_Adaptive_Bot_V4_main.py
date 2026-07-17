"""
============================================================================
XAUUSD_Adaptive_Bot_V4 -- 14-day autonomous DEMO research & paper trading
                          (GOLD only, DEMO only, single self-contained file)
============================================================================

SINGLE-FILE BUILD: cTrader embeds and executes only the main Python file of
a cBot, so this file inlines the entire V4 research system: shared V3
detectors (market structure, liquidity, supply/demand, order blocks, FVGs,
regime), the autonomous strategy space (8 exact rule archetypes x
timeframes x bounded parameters, versioned), parallel shadow portfolios,
statistical learning with risk-adjusted ranking and UCB-style selection,
the adaptive risk engine, restart-proof persistence and daily/final
reporting.  NO local folders, NO package imports, NO third-party packages,
NO network access, NO self-modifying code.

Absolute rails (validator-enforced, no live switch exists anywhere):
  * DEMO-only ("LIVE ACCOUNT BLOCKED" + stop) and GOLD-only
  * one real position; broker-side SL+TP on every order; stops never widen
  * 0.75% max risk/trade | 1.7% max combined daily loss | 5% weekly max
  * cooldown after 3 consecutive losses | volume always rounded down
  * no martingale / grid / averaging down / loss-chasing / forced targets
  * restarting never resets limits, learning state or the research clock

Research output (in <home>/Documents/XAUUSD_Adaptive_Bot_V4/):
  research_state.json, shadow_trades.csv, real_trades.csv, rejections.csv,
  daily_summary.csv, equity_history.csv, learning_log.csv,
  parameter_updates.csv, daily_report_*.txt, final_report.txt/.json
The final result is labelled: "PROVISIONAL WINNER -- NOT AUTOMATICALLY
READY FOR LIVE TRADING."  No profitability is claimed.
"""

import clr

clr.AddReference("cAlgo.API")

from cAlgo.API import *
from robot_wrapper import *

import csv
import json
import math
import os
import random
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple



#############################################################################
# SHARED CORE - DATA MODEL
#############################################################################

"""
Data model: enums and dataclasses shared by every layer.

Ported from the legacy single-file bot with the broker-specific pieces
replaced: MT5 tickets became cTrader position ids, and the MT5 symbol
specification became CTraderSymbolSpec, which is populated in the main cBot
file from the live cTrader Symbol object (tick size, tick value, pip size,
volume min/max/step in UNITS — never assumed, always read from the broker).
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ===========================================================================
# ENUMS
# ===========================================================================

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


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    PARTIAL_TP = "PARTIAL_TP"
    TRAIL_STOP = "TRAIL_STOP"
    BREAK_EVEN = "BREAK_EVEN"
    TIME_EXIT = "TIME_EXIT"
    WEEKEND_EXIT = "WEEKEND_EXIT"
    EMERGENCY = "EMERGENCY"
    MANUAL = "MANUAL"
    BROKER_CLOSED = "BROKER_CLOSED"     # closed on broker side (SL/TP hit)


class SessionName(str, Enum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    OVERLAP = "OVERLAP"      # London/NY overlap
    OFF_HOURS = "OFF_HOURS"


class LockReason(str, Enum):
    NONE = "NONE"
    DAILY_LOSS = "DAILY_LOSS"
    WEEKLY_LOSS = "WEEKLY_LOSS"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    DAILY_TARGET = "DAILY_TARGET"
    NEWS = "NEWS"
    SPREAD = "SPREAD"
    EMERGENCY = "EMERGENCY"
    MAX_TRADES_DAY = "MAX_TRADES_DAY"
    MAX_TRADES_SESSION = "MAX_TRADES_SESSION"
    SESSION_BLOCKED = "SESSION_BLOCKED"


# ===========================================================================
# MARKET DATA
# ===========================================================================

@dataclass(frozen=True)
class Candle:
    """One completed OHLC candle. time = open time (server time, tz-aware)."""
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
    """Fixed multi-timeframe roles: M15 bias/structure, M5 decision,
    M1 entry trigger, M5 management (per the trading-system spec)."""
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
    """0-100 evidence-based score. Scores are functions of market evidence
    only — never of recent P/L or distance to any daily target."""
    htf_alignment: float = 0.0        # /15  (15-minute bias alignment)
    zone_quality: float = 0.0         # /15  (supply/demand freshness etc.)
    liquidity_sweep: float = 0.0      # /15
    structure_confirmation: float = 0.0  # /15 (5m CHoCH / BOS / MSS)
    displacement: float = 0.0         # /10
    confluence: float = 0.0           # /10 (FVG / order block)
    premium_discount: float = 0.0     # /5
    session_quality: float = 0.0      # /5
    news_safety: float = 0.0          # /5
    target_quality: float = 0.0       # /5  (reward-to-risk & liquidity target)

    @property
    def total(self) -> float:
        return round(self.htf_alignment + self.zone_quality + self.liquidity_sweep
                     + self.structure_confirmation + self.displacement
                     + self.confluence + self.premium_discount
                     + self.session_quality + self.news_safety
                     + self.target_quality, 2)

    def lines(self) -> List[str]:
        """Human-readable breakdown for the log/journal."""
        return [
            f"  15m bias alignment : {self.htf_alignment:+.1f} /15",
            f"  zone quality       : {self.zone_quality:+.1f} /15",
            f"  liquidity sweep    : {self.liquidity_sweep:+.1f} /15",
            f"  5m structure conf. : {self.structure_confirmation:+.1f} /15",
            f"  displacement       : {self.displacement:+.1f} /10",
            f"  FVG/OB confluence  : {self.confluence:+.1f} /10",
            f"  premium/discount   : {self.premium_discount:+.1f} /5",
            f"  session quality    : {self.session_quality:+.1f} /5",
            f"  news safety        : {self.news_safety:+.1f} /5",
            f"  target quality     : {self.target_quality:+.1f} /5",
            f"  TOTAL              : {self.total:.1f} /100",
        ]

    def as_dict(self) -> Dict[str, float]:
        return {"htf_alignment": self.htf_alignment,
                "zone_quality": self.zone_quality,
                "liquidity_sweep": self.liquidity_sweep,
                "structure_confirmation": self.structure_confirmation,
                "displacement": self.displacement,
                "confluence": self.confluence,
                "premium_discount": self.premium_discount,
                "session_quality": self.session_quality,
                "news_safety": self.news_safety,
                "target_quality": self.target_quality,
                "total": self.total}


@dataclass
class Setup:
    setup_id: str
    model: SetupModel
    direction: Direction
    created_time: datetime            # close time of confirming candle
    signal_price: float               # price when signal was generated
    entry_price: float                # planned market entry reference
    stop_price: float
    tp1: float
    tp2: float
    runner_target: Optional[float]
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
    m1_trigger: str = ""              # which 1-minute trigger fired
    stop_reason: str = ""             # why the stop is where it is
    target_reason: str = ""           # why the target is where it is

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)

    def rr_to(self, target: float) -> float:
        if self.stop_distance <= 0:
            return 0.0
        return abs(target - self.entry_price) / self.stop_distance


@dataclass
class Trade:
    """Bot-side record of one position (cTrader holds the real position)."""
    trade_id: str
    setup: Setup
    status: TradeStatus
    volume_units: float               # remaining open volume in UNITS
    initial_volume_units: float
    risk_fraction: float              # of equity at entry decision
    risk_money: float
    entry_price: float = 0.0          # actual fill
    entry_time: Optional[datetime] = None
    stop_price: float = 0.0           # current (may tighten, never widens)
    initial_stop: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: Optional[ExitReason] = None
    profit: float = 0.0               # realised net, includes partials
    breakeven_done: bool = False
    partial_done: bool = False
    mfe: float = 0.0                  # max favourable excursion in R
    mae: float = 0.0                  # max adverse excursion in R
    bars_open: int = 0
    position_id: int = 0              # cTrader Position.Id

    @property
    def direction(self) -> Direction:
        return self.setup.direction

    def r_multiple(self) -> float:
        return self.profit / self.risk_money if self.risk_money > 0 else 0.0


# ===========================================================================
# BROKER SYMBOL SPECIFICATION (populated from the live cTrader Symbol)
# ===========================================================================

@dataclass
class CTraderSymbolSpec:
    """Actual Skilling/cTrader symbol specification — read from the platform
    at startup, never assumed. Volumes are cTrader UNITS (for gold: ounces),
    not MT5 lots."""
    name: str
    digits: int
    tick_size: float                  # smallest price increment
    tick_value: float                 # account-currency value of one tick per 1 unit
    pip_size: float
    pip_value: float                  # account-currency value of one pip per 1 unit
    volume_min: float                 # in units
    volume_max: float                 # in units
    volume_step: float                # in units
    spread_points: float = 0.0        # live spread in points (tick_size units)
    trade_allowed: bool = True

    @property
    def point(self) -> float:
        """One 'point' = one tick of price. Gold on cTrader is usually
        quoted with tick size 0.01, so 60 points = $0.60 of price."""
        return self.tick_size

    def money_per_price_unit_per_unit(self) -> float:
        """Account-currency P/L of a 1.0 price move for 1 UNIT of volume."""
        if self.tick_size <= 0:
            return 0.0
        return self.tick_value / self.tick_size

    def round_volume_down(self, volume: float) -> float:
        """Round DOWN to the broker volume step; 0 if below minimum."""
        if self.volume_step <= 0 or volume < self.volume_min:
            return 0.0
        stepped = math.floor((volume + 1e-9) / self.volume_step) * self.volume_step
        stepped = min(stepped, self.volume_max)
        if stepped < self.volume_min:
            return 0.0
        decimals = max(0, -int(math.floor(math.log10(self.volume_step))))
        return round(stepped, decimals)

    def valid(self) -> Tuple[bool, str]:
        if self.tick_size <= 0:
            return False, "tick size is zero/unknown"
        if self.tick_value <= 0:
            return False, "tick value is zero/unknown"
        if self.volume_min <= 0 or self.volume_step <= 0:
            return False, "volume min/step is zero/unknown"
        if self.volume_max < self.volume_min:
            return False, "volume max below volume min"
        if not self.trade_allowed:
            return False, "symbol not tradeable"
        return True, "ok"


#############################################################################
# SHARED CORE - V3 DETECTOR CONFIG (used by shared detectors)
#############################################################################

"""
Configuration — every tunable of the bot with SAFE defaults.

Edit this file to configure the bot; the values here are the single source
of truth.  Optionally, the main cBot exposes the most important knobs as
cTrader UI parameters (declared in the auto-generated .cs file, see
README.md): when a UI parameter with the matching name exists it OVERRIDES
the value here.

There are NO credentials in this file and no live-trading switch anywhere:
the bot verifies at startup that the connected account is a DEMO account
and stops immediately otherwise.
"""


@dataclass
class Config:
    # ---- symbol -----------------------------------------------------------
    # The bot refuses to run on anything that is not in this gold allowlist.
    # Skilling's gold symbol is usually "XAUUSD"; some brokers use "GOLD".
    allowed_gold_symbols: Tuple[str, ...] = (
        "GOLD", "XAUUSD", "XAU/USD", "XAUUSD.", "GOLD.", "XAUUSDM",
        "XAUUSD-STD", "GOLD-STD", "XAUUSD.PRO", "XAUUSD.RAW")

    # ---- risk policy (fractions of equity, e.g. 0.0025 == 0.25%) ----------
    max_risk_per_trade: float = 0.0025        # 0.25% — HARD ceiling per trade
    max_daily_loss: float = 0.01              # 1% realised+floating => day lock
    max_weekly_drawdown: float = 0.05         # extra guard beyond the spec
    max_consecutive_losses: int = 3           # pause after a losing streak
    max_positions: int = 1                    # one open GOLD position, ever
    max_trades_per_day: int = 3
    max_trades_per_session: int = 2
    daily_profit_hard_stop: float = 0.03      # bank a +3% day, stop entries

    # ---- reward:risk -------------------------------------------------------
    min_rr: float = 1.5                       # minimum net reward-to-risk
    preferred_rr: float = 2.0                 # target quality scoring anchor
    preferred_rr_cap: float = 4.0             # don't project fantasy targets

    # ---- costs & execution quality -----------------------------------------
    max_spread_points: float = 60.0           # 60 pts = $0.60 on gold; reject above
    normal_spread_points: float = 35.0        # regime abnormal-spread check
    slippage_buffer_points: float = 15.0      # assumed in sizing
    commission_per_unit: float = 0.0          # Skilling gold is spread-only by
                                              # default; set if your account differs

    # ---- structure / detection parameters ----------------------------------
    swing_left: int = 2
    swing_right: int = 2
    external_swing_left: int = 3              # for H4/D1 swing detection
    external_swing_right: int = 3
    atr_period: int = 14
    displacement_atr_mult: float = 1.2
    displacement_body_ratio: float = 0.55
    bos_use_close: bool = True                # close-based breaks (no wick BOS)
    eq_level_atr_tol: float = 0.12            # equal highs/lows tolerance
    zone_base_max_candles: int = 5
    zone_base_body_atr: float = 0.60
    zone_max_touches: int = 2                 # more touches -> stale zone
    zone_max_age_candles: int = 400
    fvg_min_size_atr: float = 0.15
    ob_require_structure_link: bool = True
    premium_discount_buffer: float = 0.05     # 45-55% counts as equilibrium
    extension_max_atr: float = 3.0            # "don't chase" rule
    retest_max_candles: int = 12
    range_edge_fraction: float = 0.25
    min_stop_atr: float = 0.35                # reject stops inside noise
    max_stop_atr: float = 3.5                 # reject stops absurdly wide
    stop_buffer_atr: float = 0.25             # buffer beyond structure

    # ---- scoring / gating ---------------------------------------------------
    min_score: float = 70.0                   # minimum setup score (of 100)
    countertrend_min_score: float = 80.0      # reversals need more proof
    asia_min_score: float = 85.0              # Asia = low liquidity, be picky
    allow_reversals: bool = False             # trades against 15m bias OFF
    require_m1_trigger: bool = True           # 1-minute trigger gate
    m1_trigger_max_age: int = 5               # trigger must be this recent (M1 bars)

    # ---- sessions (SERVER-TIME hours; see README about timezone) -----------
    # cTrader Python cBots receive Server.Time. For most cTrader brokers the
    # server time is UTC — VERIFY against your Skilling demo and adjust
    # server_utc_offset_hours if the platform clock differs from UTC.
    server_utc_offset_hours: int = 0
    asia_enabled: bool = True                 # only with asia_min_score quality
    london_enabled: bool = True
    newyork_enabled: bool = True
    asia_start: int = 0
    asia_end: int = 7
    london_start: int = 7
    london_end: int = 16
    ny_start: int = 12
    ny_end: int = 21
    avoid_rollover: Tuple[int, int] = (21, 23)    # no entries in this window
    friday_last_entry_hour: int = 15
    friday_flat_hour: int = 20                # close positions before weekend
    monday_first_entry_hour: int = 2
    weekend_hold_allowed: bool = False

    # ---- news protection (manual windows ONLY — see news_filter.py) --------
    # cTrader Python cBots have no reliable built-in economic calendar and
    # this bot performs NO network calls. News protection is therefore
    # schedule-based and manual: enter upcoming events below. NFP can be
    # auto-blocked via its regular first-Friday schedule.
    news_enabled: bool = True
    news_block_before_min: int = 30
    news_block_after_min: int = 30
    block_nfp: bool = True                    # first Friday of month (see below)
    nfp_hour_utc: int = 12                    # 12:30 UTC release stub hour
    nfp_minute_utc: int = 30
    block_fomc: bool = True                   # uses fomc_events list below
    block_cpi: bool = True                    # uses cpi_events list below
    block_speeches: bool = True               # uses speech_events list below
    # Event lists: "YYYY-MM-DDTHH:MM" in UTC. THESE MUST BE MAINTAINED BY
    # YOU — the bot never invents events. Examples (commented):
    #   fomc_events: ("2026-07-29T18:00",)
    fomc_events: Tuple[str, ...] = ()
    cpi_events: Tuple[str, ...] = ()
    speech_events: Tuple[str, ...] = ()
    # Free-form extra blackouts: "YYYY-MM-DDTHH:MM/YYYY-MM-DDTHH:MM" (UTC)
    manual_blackouts: Tuple[str, ...] = ()

    # ---- trade management (all OFF by default until tested) ----------------
    breakeven_enabled: bool = False
    breakeven_r: float = 1.0                  # move to BE only at >= +1R
    breakeven_needs_structure: bool = True    # and a confirmed swing in profit
    trailing_enabled: bool = False
    trail_atr_mult: float = 1.6
    partial_tp_enabled: bool = False
    partial_r: float = 1.7
    partial_fraction: float = 0.40
    time_exit_hours: float = 30.0             # safety exit for stale trades
    max_bars_no_progress: int = 60

    # ---- operations ---------------------------------------------------------
    emergency_stop: bool = False              # config kill switch (no entries)
    emergency_stop_file: str = "EMERGENCY_STOP.txt"   # file kill switch
    debug_logging: bool = False
    strict_mode: bool = True                  # anything unverifiable => no trade
    journal_enabled: bool = True
    journal_dir: str = ""                     # "" = <home>/Documents/XAUUSD_Adaptive_Bot
    order_fail_cooldown_min: int = 15         # no re-tries spam after a failure

    # ---- analysis windows (completed candles per timeframe) -----------------
    window_m1: int = 700
    window_m5: int = 600
    window_m15: int = 450
    window_h1: int = 320
    window_h4: int = 260
    window_d1: int = 220
    min_candles_required: int = 120


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

        if not 0 < c.max_risk_per_trade <= 0.0025:
            err("max_risk_per_trade must be in (0, 0.0025] — 0.25% is the "
                "hard ceiling for this demo-only bot and cannot be raised")
        if not 0 < c.max_daily_loss <= 0.01:
            err("max_daily_loss must be in (0, 0.01] — 1% is the hard ceiling")
        if not 0 < c.max_weekly_drawdown <= 0.20:
            err("max_weekly_drawdown must be in (0, 0.20]")
        if c.max_consecutive_losses < 1:
            err("max_consecutive_losses must be >= 1")
        if c.max_positions != 1:
            err("max_positions must be exactly 1 (one GOLD position rule)")
        if c.max_trades_per_day < 1 or c.max_trades_per_day > 3:
            err("max_trades_per_day must be 1..3")
        if c.max_trades_per_session < 1:
            err("max_trades_per_session must be >= 1")
        if c.min_rr < 1.5:
            err("min_rr below 1.5 is not acceptable")
        if c.min_score < 70:
            err("min_score below 70 violates the no-trade threshold")
        if c.countertrend_min_score < c.min_score:
            err("countertrend_min_score must be >= min_score")
        if c.swing_left < 1 or c.swing_right < 1:
            err("swing detection needs at least 1 candle each side")
        if c.atr_period < 5:
            err("atr_period too small to be meaningful")
        if c.max_spread_points <= 0:
            err("max_spread_points must be positive")
        if c.stop_buffer_atr < 0:
            err("stop_buffer_atr cannot be negative")
        if not (0 < c.min_stop_atr < c.max_stop_atr):
            err("need 0 < min_stop_atr < max_stop_atr")
        if c.breakeven_r < 0.5:
            err("breakeven_r below 0.5R moves to break-even too early")
        if c.partial_fraction <= 0 or c.partial_fraction >= 1:
            err("partial_fraction must be inside (0, 1)")
        for name in ("asia", "london", "ny"):
            s = getattr(c, f"{name}_start")
            e = getattr(c, f"{name}_end")
            if not (0 <= s < 24 and 0 < e <= 24 and s < e):
                err(f"session hours invalid for {name}: {s}-{e}")
        if not (c.asia_enabled or c.london_enabled or c.newyork_enabled):
            warn("all sessions disabled — the bot will never trade")
        if c.allow_reversals:
            warn("allow_reversals=True — counter-bias trades enabled "
                 f"(min score {c.countertrend_min_score})")
        if not c.news_enabled:
            warn("news protection disabled — not recommended")
        if c.news_enabled and not (c.fomc_events or c.cpi_events
                                   or c.manual_blackouts):
            warn("news protection is manual/schedule-based and no FOMC/CPI "
                 "dates are configured — only the NFP first-Friday rule and "
                 "spread locks protect you; add this month's dates to "
                 "fomc_events / cpi_events in config.py")
        if c.breakeven_enabled or c.trailing_enabled or c.partial_tp_enabled:
            warn("management features enabled before being tested — the spec "
                 "recommends leaving them OFF until backtests pass")
        return not self.errors

    def report(self) -> str:
        lines = [f"CONFIG ERROR: {e}" for e in self.errors]
        lines += [f"CONFIG WARNING: {w}" for w in self.warnings]
        return "\n".join(lines) if lines else "config OK"


#############################################################################
# SHARED CORE - MATH HELPERS
#############################################################################

"""
Small pure-math utilities shared by the strategy modules.
All functions operate on COMPLETED candles only and never look ahead:
value at index i uses candles up to and including i.
"""


UTC = timezone.utc


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
    return ((candles[-1].close - candles[-(lookback + 1)].close)
            / candles[-(lookback + 1)].close)


def is_displacement(candle: Candle, atr: float,
                    atr_mult: float, body_ratio: float) -> bool:
    """Objective displacement: large body relative to ATR, dominant body."""
    if atr <= 0:
        return False
    return (candle.body >= atr_mult * atr
            and candle.body_ratio >= body_ratio)


def tf_bucket_start(t: datetime, tf: Timeframe) -> datetime:
    """Timezone-aware open time of the tf bucket containing t."""
    if tf == Timeframe.W1:
        d = t.date() - timedelta(days=t.weekday())  # Monday
        return datetime(d.year, d.month, d.day, tzinfo=t.tzinfo)
    if tf == Timeframe.D1:
        return datetime(t.year, t.month, t.day, tzinfo=t.tzinfo)
    mins = tf.minutes
    total = t.hour * 60 + t.minute
    start = (total // mins) * mins
    return datetime(t.year, t.month, t.day, start // 60, start % 60,
                    tzinfo=t.tzinfo)


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


#############################################################################
# SHARED - MARKET STRUCTURE
#############################################################################

"""
Market structure: fractal swings, trend (HH/HL vs LH/LL), BOS / CHoCH / MSS
events, dealing range and premium/discount classification.

Everything works on COMPLETED candles and is confirmation-delayed so nothing
ever repaints: a swing only exists once `swing_right` candles have closed
after it.
"""


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
      * MSS            : a CHoCH whose breaking candle shows displacement —
                         a graded, stronger shift.
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
                    last_high = s
                    pending_break_high = s
                else:
                    last_low = s
                    pending_break_low = s
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
            disp = is_displacement(c, a, cfg.displacement_atr_mult,
                                   cfg.displacement_body_ratio)

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
        if trend in (TrendState.UNDEFINED, TrendState.RANGING):
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


#############################################################################
# SHARED - LIQUIDITY
#############################################################################

"""
Liquidity: resting pools (equal highs/lows, swing highs/lows, previous
day/week highs/lows, session highs/lows) and OBJECTIVE sweep detection.

Sweep definition: price TRADES THROUGH a recognised level (high above
buy-side liquidity / low below sell-side liquidity) AND EITHER the candle
closes back on the original side of the level OR a displacement candle
moves away within SWEEP_CONFIRM_CANDLES.  A wick through alone is NOT a
sweep, and a touch alone never justifies an entry.
"""


class LiquidityDetector:

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

        # session highs/lows supplied by the SessionManager
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
                    if not is_displacement(ck, a, cfg.displacement_atr_mult,
                                           cfg.displacement_body_ratio):
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


#############################################################################
# SHARED - SUPPLY/DEMAND + ORDER BLOCKS
#############################################################################

"""
Supply & demand zones and order blocks.

Zone = leg-in -> base -> leg-out:
  * base: 1..zone_base_max_candles consecutive candles whose bodies are
    all <= zone_base_body_atr * ATR.
  * leg-out: the first candle after the base shows displacement; its
    direction defines SUPPLY (down) or DEMAND (up).
  * leg-in direction + leg-out direction give RBD/DBR/DBD/RBR.
Boundaries: distal = extreme of the base range, proximal = the base body
edge nearest to the departure. Invalidation = close through distal.
Freshness decays with age and touches; heavily-touched zones score 0.

Order block = the LAST opposite-coloured candle immediately before a
displacement move that (a) produced BOS/CHoCH/MSS or (b) swept liquidity.
Plain opposite candles are NOT order blocks.
"""


class SupplyDemandDetector:

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
            if not is_displacement(out_c, a_out, cfg.displacement_atr_mult,
                                   cfg.displacement_body_ratio):
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
            zones.append(Zone(zone_id=new_id("zone"), kind=kind, pattern=pattern,
                              upper=upper, lower=lower, timeframe=self.tf,
                              created_time=candles[base_start].time,
                              created_index=base_start,
                              displacement_score=disp_score))
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
            # structure break caused shortly after the departure
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


class OrderBlockDetector:

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
        sweep_idx = {sv.index for sv in (sweeps or [])}
        blocks: List[OrderBlock] = []
        for i in range(cfg.atr_period, n):
            c = candles[i]
            a = atr[i]
            if not is_displacement(c, a, cfg.displacement_atr_mult,
                                   cfg.displacement_body_ratio):
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


#############################################################################
# SHARED - FAIR VALUE GAPS
#############################################################################

"""
Strict three-candle fair value gaps (imbalances).

Bullish FVG at middle candle i: low[i+1] > high[i-1]  (gap up)
Bearish FVG at middle candle i: high[i+1] < low[i-1]  (gap down)
Gap must be >= fvg_min_size_atr * ATR. Fill state is tracked candle by
candle (UNFILLED / PARTIAL / MITIGATED). An FVG is confluence only — it is
never enough to enter a trade by itself.
"""


class FVGDetector:

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
                    from_displacement=is_displacement(
                        c_mid, a, cfg.displacement_atr_mult,
                        cfg.displacement_body_ratio)))
            elif c_next.high < c_prev.low and (c_prev.low - c_next.high) >= min_size:
                gaps.append(FairValueGap(
                    fvg_id=new_id("fvg"), direction=Direction.SHORT,
                    upper=c_prev.low, lower=c_next.high, timeframe=self.tf,
                    created_time=c_mid.time, created_index=i,
                    from_displacement=is_displacement(
                        c_mid, a, cfg.displacement_atr_mult,
                        cfg.displacement_body_ratio)))
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


#############################################################################
# SHARED - MARKET REGIME
#############################################################################

"""
Objective market-regime classification from structure + volatility inputs.
Determines which of the six entry models are allowed to run right now.
"""


class MarketRegimeDetector:

    ATR_HISTORY = 200
    EFF_LOOKBACK = 20
    RANGE_LOOKBACK = 40

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def classify(self, candles: Sequence[Candle],
                 structure: StructureState,
                 spread_points: float = 0.0,
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
                                 f"spread {spread_points:.0f}pt > max "
                                 f"{cfg.max_spread_points:.0f}pt")
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
                        if ev.kind in (StructureEventKind.CHOCH,
                                       StructureEventKind.MSS)]
        disp_count = sum(
            1 for i in range(len(candles) - 6, len(candles))
            if i >= 0 and is_displacement(candles[i], atr_all[i],
                                          cfg.displacement_atr_mult,
                                          cfg.displacement_body_ratio))

        # compression: low vol percentile + narrow range
        if atr_pct <= 0.25 and width_atr < 8.0 and eff < 0.25:
            return RegimeReading(Regime.COMPRESSION, trend, 0.7, atr_now,
                                 atr_pct, eff,
                                 f"ATR pct {atr_pct:.2f}, width {width_atr:.1f} "
                                 f"ATR, eff {eff:.2f}")
        # expansion: vol percentile spiking + displacement burst
        if atr_pct >= 0.85 and disp_count >= 2:
            return RegimeReading(Regime.EXPANSION, trend, 0.7, atr_now,
                                 atr_pct, eff,
                                 f"ATR pct {atr_pct:.2f} with {disp_count} "
                                 f"displacement candles")
        # reversal attempt: fresh counter-trend CHoCH/MSS
        if recent_choch:
            return RegimeReading(Regime.REVERSAL_ATTEMPT, trend, 0.6, atr_now,
                                 atr_pct, eff,
                                 f"recent {recent_choch[-1].kind.value} "
                                 f"against prior trend")
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
                             f"no directional structure, eff {eff:.2f}, "
                             f"width {width_atr:.1f} ATR")


#############################################################################
# SHARED - SESSIONS
#############################################################################

"""
Session tracking and entry-window gating.

TIMEZONE MODEL (read this):
Session hours in config.py are expressed in UTC.  The main cBot converts
cTrader Server.Time to UTC using cfg.server_utc_offset_hours before
anything here is called — for most cTrader brokers the server clock IS
UTC, so the default offset of 0 is correct, but verify it once against
your Skilling demo (compare the platform clock with an online UTC clock)
and adjust the offset if needed.  All candle times flow through the same
conversion, so sessions, news windows and candles always agree.

Each session (Asia / London / New York) can be switched off individually.
Session highs/lows are tracked per day from completed candles and feed the
liquidity engine (session-liquidity sweeps and targets).
"""


@dataclass
class SessionRange:
    name: SessionName
    day: date
    high: Optional[float] = None
    low: Optional[float] = None


class SessionManager:

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ranges: Dict[Tuple[date, SessionName], SessionRange] = {}

    def session_at(self, t: datetime) -> SessionName:
        h = t.hour
        c = self.cfg
        in_london = c.london_start <= h < c.london_end
        in_ny = c.ny_start <= h < c.ny_end
        if in_london and in_ny:
            return SessionName.OVERLAP
        if in_london:
            return SessionName.LONDON
        if in_ny:
            return SessionName.NEW_YORK
        if c.asia_start <= h < c.asia_end:
            return SessionName.ASIA
        return SessionName.OFF_HOURS

    def session_enabled(self, session: SessionName) -> bool:
        c = self.cfg
        if session == SessionName.ASIA:
            return c.asia_enabled
        if session == SessionName.LONDON:
            return c.london_enabled
        if session == SessionName.NEW_YORK:
            return c.newyork_enabled
        if session == SessionName.OVERLAP:
            return c.london_enabled or c.newyork_enabled
        return False

    # -- session range tracking ------------------------------------------------
    def update_ranges(self, candle: Candle) -> None:
        """Feed each completed base candle to build session highs/lows."""
        t = candle.time
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
        """Return (allowed, reason). Calendar rules in UTC."""
        c = self.cfg
        wd = t.weekday()  # Mon=0 ... Sun=6
        if wd >= 5:
            return False, "weekend"
        if wd == 4 and t.hour >= c.friday_last_entry_hour:
            return False, f"late Friday (after {c.friday_last_entry_hour}:00 UTC)"
        if wd == 0 and t.hour < c.monday_first_entry_hour:
            return False, "early Monday instability window"
        lo, hi = c.avoid_rollover
        if lo <= t.hour < hi:
            return False, f"daily rollover window {lo}-{hi} UTC"
        sess = self.session_at(t)
        if sess == SessionName.OFF_HOURS:
            return False, "off-hours / illiquid"
        if not self.session_enabled(sess):
            return False, f"session {sess.value} disabled in config"
        return True, sess.value

    def near_weekend_flat(self, t: datetime) -> bool:
        return t.weekday() == 4 and t.hour >= self.cfg.friday_flat_hour


#############################################################################
# SHARED - NEWS PROTECTION
#############################################################################

"""
News protection — HONEST LIMITATION FIRST:

cTrader Python cBots have no reliable built-in economic calendar, and this
bot makes NO network calls and uses NO API keys.  News protection here is
therefore SCHEDULE-BASED AND MANUAL — the bot never fabricates events and
never pretends to have live coverage:

  * NFP        : blocked automatically via its regular schedule (first
                 Friday of the month at nfp_hour_utc:nfp_minute_utc).
                 Occasionally the BLS shifts the date — around holidays,
                 verify manually.
  * FOMC / CPI / central-bank speeches: blocked ONLY if you maintain the
                 date lists in config.py ("YYYY-MM-DDTHH:MM", UTC).
  * Extra manual blackout windows: "start/end" ISO pairs in UTC.

Every rejection caused by news is logged with the exact event/window that
caused it.  protection_complete() always returns False so the setup scorer
can never award the full news-safety score to manual-only protection.
"""


@dataclass
class NewsEvent:
    time: datetime
    title: str
    category: str        # "NFP" / "FOMC" / "CPI" / "SPEECH" / "MANUAL"


def _parse_iso_utc(s: str, tzinfo) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(s.strip())
        if t.tzinfo is None:
            t = t.replace(tzinfo=tzinfo)
        return t
    except ValueError:
        return None


def first_friday(year: int, month: int) -> int:
    """Day-of-month of the first Friday."""
    d = datetime(year, month, 1)
    offset = (4 - d.weekday()) % 7      # Friday = 4
    return 1 + offset


class NewsFilter:

    def __init__(self, cfg: Config, tzinfo):
        """tzinfo: timezone used for all bot times (UTC)."""
        self.cfg = cfg
        self.tz = tzinfo
        self._events: List[NewsEvent] = []
        self._windows: List[Tuple[datetime, datetime, str]] = []
        self._malformed: List[str] = []
        self._load()

    def _load(self) -> None:
        c = self.cfg
        sources = (("FOMC", c.fomc_events, c.block_fomc),
                   ("CPI", c.cpi_events, c.block_cpi),
                   ("SPEECH", c.speech_events, c.block_speeches))
        for category, entries, enabled in sources:
            if not enabled:
                continue
            for s in entries:
                t = _parse_iso_utc(s, self.tz)
                if t is None:
                    self._malformed.append(f"{category}: {s}")
                    continue
                self._events.append(NewsEvent(t, f"{category} (configured)",
                                              category))
        for w in c.manual_blackouts:
            try:
                a, b = w.split("/")
                t0 = _parse_iso_utc(a, self.tz)
                t1 = _parse_iso_utc(b, self.tz)
                if t0 and t1 and t1 > t0:
                    self._windows.append((t0, t1, "manual blackout"))
                else:
                    self._malformed.append(f"MANUAL: {w}")
            except ValueError:
                self._malformed.append(f"MANUAL: {w}")

    def malformed_entries(self) -> List[str]:
        """Config entries that could not be parsed (report at startup)."""
        return list(self._malformed)

    def _nfp_event_for(self, t: datetime) -> Optional[NewsEvent]:
        if not self.cfg.block_nfp:
            return None
        day = first_friday(t.year, t.month)
        nfp = datetime(t.year, t.month, day, self.cfg.nfp_hour_utc,
                       self.cfg.nfp_minute_utc, tzinfo=self.tz)
        return NewsEvent(nfp, "NFP (first-Friday schedule)", "NFP")

    def blackout(self, t: datetime) -> Tuple[bool, str]:
        """(blocked, reason) for time t. t must be UTC (tz-aware)."""
        c = self.cfg
        if not c.news_enabled:
            return False, "news protection disabled"
        for t0, t1, label in self._windows:
            if t0 <= t <= t1:
                return True, (f"{label} {t0:%Y-%m-%d %H:%M}-"
                              f"{t1:%H:%M} UTC")
        before = timedelta(minutes=c.news_block_before_min)
        after = timedelta(minutes=c.news_block_after_min)
        candidates = list(self._events)
        nfp = self._nfp_event_for(t)
        if nfp is not None:
            candidates.append(nfp)
            # also consider next month's NFP when t is near month end
            if t.month == 12:
                nxt = t.replace(year=t.year + 1, month=1, day=1)
            else:
                nxt = t.replace(month=t.month + 1, day=1)
            day = first_friday(nxt.year, nxt.month)
            candidates.append(NewsEvent(
                datetime(nxt.year, nxt.month, day, c.nfp_hour_utc,
                         c.nfp_minute_utc, tzinfo=self.tz),
                "NFP (first-Friday schedule)", "NFP"))
        for ev in candidates:
            if ev.time - before <= t <= ev.time + after:
                return True, (f"{ev.title} at {ev.time:%Y-%m-%d %H:%M} UTC "
                              f"(blocking {c.news_block_before_min}m before / "
                              f"{c.news_block_after_min}m after)")
        return False, "no scheduled news in window"

    def protection_complete(self) -> bool:
        """Manual/schedule-based protection is NEVER complete — this
        deliberately caps the news-safety score and is logged at startup."""
        return False

    def upcoming(self, t: datetime, within_hours: float = 24) -> List[NewsEvent]:
        horizon = t + timedelta(hours=within_hours)
        out = [e for e in self._events if t <= e.time <= horizon]
        nfp = self._nfp_event_for(t)
        if nfp and t <= nfp.time <= horizon:
            out.append(nfp)
        return sorted(out, key=lambda e: e.time)


#############################################################################
# SHARED - SPREAD FILTER
#############################################################################

"""
Spread quality gate.  Spread is measured in points (ticks of price — for
gold with tick size 0.01, 60 points = $0.60) and compared against the
configured maximum.  A spread above the limit vetoes new entries; the
regime detector independently flags ABNORMAL_SPREAD which disables all
entry models as well.
"""


class SpreadFilter:

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def check(self, spread_points: float) -> Tuple[bool, str]:
        """(ok, reason).  spread_points <= 0 means 'unknown' and fails
        closed — never trade without a verifiable spread."""
        if spread_points <= 0:
            return False, "spread unknown/zero — cannot verify execution cost"
        if spread_points > self.cfg.max_spread_points:
            return False, (f"spread {spread_points:.0f} pts > max "
                           f"{self.cfg.max_spread_points:.0f} pts")
        return True, (f"spread {spread_points:.0f} pts <= max "
                      f"{self.cfg.max_spread_points:.0f} pts")


#############################################################################
# SHARED - POSITION SIZING
#############################################################################

"""
Position sizing from TRUE monetary risk using the live cTrader symbol
specification (tick size, tick value, volume min/max/step in UNITS).

Rules enforced here:
  * volume is ALWAYS rounded DOWN to the broker volume step;
  * if the minimum broker volume already risks more than allowed, the
    trade is REJECTED (never forced);
  * risk never exceeds cfg.max_risk_per_trade (0.25% hard ceiling);
  * the cost model includes spread + slippage buffer + optional commission
    so the sized risk is the worst-case loss, not the optimistic one;
  * a degenerate/unverifiable symbol spec rejects the trade outright.
"""


@dataclass
class SizingResult:
    volume_units: float
    risk_money: float                 # actual worst-case risk at this volume
    intended_risk_money: float
    risk_fraction_actual: float
    stop_points: float
    cost_estimate: float
    rejected: bool = False
    reason: str = ""


class PositionSizer:

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def size(self, spec: CTraderSymbolSpec, equity: float,
             risk_fraction: float, entry: float, stop: float) -> SizingResult:
        cfg = self.cfg
        risk_fraction = min(risk_fraction, cfg.max_risk_per_trade)
        ok, why = spec.valid()
        if not ok:
            return SizingResult(0, 0, 0, 0, 0, 0, True,
                                f"symbol spec unverifiable: {why}")
        stop_dist = abs(entry - stop)
        if stop_dist <= 0 or equity <= 0:
            return SizingResult(0, 0, 0, 0, 0, 0, True, "invalid stop/equity")
        money_per_unit = spec.money_per_price_unit_per_unit()
        if money_per_unit <= 0:
            return SizingResult(0, 0, 0, 0, 0, 0, True,
                                "symbol spec has no tick value/size")
        intended = equity * risk_fraction
        # worst-case loss per UNIT: stop distance + slippage + spread + fees
        slip = cfg.slippage_buffer_points * spec.point
        spread_cost = spec.spread_points * spec.point
        loss_per_unit = (stop_dist + slip + spread_cost) * money_per_unit \
            + cfg.commission_per_unit
        if loss_per_unit <= 0:
            return SizingResult(0, 0, 0, 0, 0, 0, True, "degenerate cost model")
        raw_volume = intended / loss_per_unit
        volume = spec.round_volume_down(raw_volume)
        if volume <= 0:
            min_risk = spec.volume_min * loss_per_unit
            return SizingResult(
                0, 0, intended, 0, stop_dist / spec.point,
                cfg.commission_per_unit * spec.volume_min, True,
                f"minimum volume {spec.volume_min} units would risk "
                f"{min_risk:.2f} ({min_risk / equity:.2%}) > intended "
                f"{intended:.2f} ({risk_fraction:.2%}) — trade rejected, "
                f"never forced")
        actual = volume * loss_per_unit
        # rounding down can never exceed intended risk; double-check anyway
        if actual > intended * 1.0001:
            return SizingResult(0, 0, intended, 0, stop_dist / spec.point,
                                0, True, "sizing exceeded intended risk")
        return SizingResult(
            volume_units=volume, risk_money=actual,
            intended_risk_money=intended,
            risk_fraction_actual=actual / equity,
            stop_points=stop_dist / spec.point,
            cost_estimate=cfg.commission_per_unit * volume
            + (slip + spread_cost) * money_per_unit * volume)

    def risk_fraction_for(self, grade_value: str, score: float) -> float:
        """Risk by setup grade — but ALWAYS capped at max_risk_per_trade
        (0.25%). Better grades use the full allowance; B setups use less.
        There is no path above the cap: no martingale, no doubling after
        losses, no recovery mode."""
        cap = self.cfg.max_risk_per_trade
        if grade_value == "A+":
            return cap
        if grade_value == "A":
            return cap * 0.85
        if grade_value == "B":
            return cap * 0.7
        return 0.0


#############################################################################
# SHARED - DAILY LOSS GUARD (base)
#############################################################################

"""
Daily / weekly capital-protection locks.

At the start of each trading day the guard snapshots starting equity and
balance, resets the daily trade counter and the realised-loss accumulator.
New entries stop for the REST OF THE DAY when any of these trips:

  * realised daily loss reaches max_daily_loss (default 1%),
  * realised + floating daily loss reaches max_daily_loss,
  * the daily trade cap is reached,
  * the emergency stop is enabled,
  * (extra guards) weekly drawdown or a consecutive-loss streak.

Locks clear ONLY on the natural boundary (next trading day / week) — never
intra-period.  State is held in memory and REBUILT ON RESTART from the
broker's own trade history (the main cBot replays today's closed trades
into the guard at startup), so a mid-day restart cannot bypass the lock.
"""


@dataclass
class DayState:
    day: str                          # ISO date key
    start_equity: float
    start_balance: float
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


class DailyLossGuard:

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.day: Optional[DayState] = None
        self.week: Optional[WeekState] = None
        self.consecutive_losses: int = 0
        self.emergency: bool = False
        self._last_lock: LockReason = LockReason.NONE

    # -- period keys -----------------------------------------------------------
    @staticmethod
    def _day_key(t: datetime) -> str:
        return t.date().isoformat()

    @staticmethod
    def _week_key(t: datetime) -> str:
        iso = t.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    def roll(self, t: datetime, equity: float,
             balance: float) -> Tuple[bool, str]:
        """Advance day/week state. Returns (new_day_started, day_key)."""
        new_day = False
        dk = self._day_key(t)
        if self.day is None or self.day.day != dk:
            self.day = DayState(day=dk, start_equity=equity,
                                start_balance=balance, min_equity=equity)
            new_day = True
        wk = self._week_key(t)
        if self.week is None or self.week.week != wk:
            self.week = WeekState(week=wk, start_equity=equity,
                                  min_equity=equity)
        self.day.min_equity = min(self.day.min_equity or equity, equity)
        self.week.min_equity = min(self.week.min_equity or equity, equity)
        return new_day, dk

    # -- trade outcome bookkeeping ----------------------------------------------
    def register_open(self, session: SessionName) -> None:
        if self.day:
            self.day.trades_opened += 1
            key = session.value
            self.day.session_trades[key] = self.day.session_trades.get(key, 0) + 1

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

    # -- lock evaluation ----------------------------------------------------------
    def lock_reason(self, equity: float, unrealised: float = 0.0,
                    session: Optional[SessionName] = None) -> LockReason:
        cfg = self.cfg
        if self.emergency or cfg.emergency_stop:
            return LockReason.EMERGENCY
        if self.day:
            base = self.day.start_equity or equity
            if base > 0:
                realised_frac = self.day.realised / base
                combined_frac = (self.day.realised + min(0.0, unrealised)) / base
                if realised_frac <= -cfg.max_daily_loss:
                    return LockReason.DAILY_LOSS
                if combined_frac <= -cfg.max_daily_loss:
                    return LockReason.DAILY_LOSS
                if realised_frac >= cfg.daily_profit_hard_stop:
                    return LockReason.DAILY_TARGET
            if self.day.trades_opened >= cfg.max_trades_per_day:
                return LockReason.MAX_TRADES_DAY
            if session and self.day.session_trades.get(session.value, 0) \
                    >= cfg.max_trades_per_session:
                return LockReason.MAX_TRADES_SESSION
        if self.week and self.week.start_equity > 0:
            wk_dd = (self.week.start_equity
                     - min(self.week.min_equity,
                           equity + min(0.0, unrealised))) \
                / self.week.start_equity
            wk_pl = (self.week.realised + min(0.0, unrealised)) \
                / self.week.start_equity
            if wk_dd >= cfg.max_weekly_drawdown or wk_pl <= -cfg.max_weekly_drawdown:
                return LockReason.WEEKLY_LOSS
        if self.consecutive_losses >= cfg.max_consecutive_losses:
            return LockReason.CONSECUTIVE_LOSSES
        return LockReason.NONE

    def lock_changed(self, lock: LockReason) -> bool:
        """True the first time a new lock state appears (for one-shot logs)."""
        changed = lock != self._last_lock
        self._last_lock = lock
        return changed

    def describe(self, equity: float) -> str:
        if not self.day:
            return "day state not initialised"
        base = self.day.start_equity or 1.0
        return (f"day {self.day.day}: start equity {self.day.start_equity:.2f}, "
                f"realised {self.day.realised:+.2f} "
                f"({self.day.realised / base:+.2%}), "
                f"trades {self.day.trades_opened}/{self.cfg.max_trades_per_day}, "
                f"wins {self.day.wins} losses {self.day.losses}, "
                f"consec. losses {self.consecutive_losses}")


#############################################################################
# V4 - RESEARCH CONFIG
#############################################################################

"""
V4 configuration — every policy knob of the 14-day research system.

The absolute safety rails live here and are enforced by the validator:
demo-only and gold-only are checked in the main cBot; risk is hard-capped
at 0.75% per trade, 1.7% combined daily loss, 5% weekly drawdown, a
cooldown after 3 consecutive losses, one real position, volume rounded
down, and there is no live-trading switch anywhere.

V4Config also carries the detector parameters (same names as V3's Config)
so the reused adaptive_bot detectors, session manager, news filter,
spread filter, position sizer and daily guard all accept it directly.
"""


@dataclass
class V4Config:
    # ---- symbol lock --------------------------------------------------------
    allowed_gold_symbols: Tuple[str, ...] = (
        "GOLD", "XAUUSD", "XAU/USD", "XAUUSD.", "GOLD.", "XAUUSDM",
        "XAUUSD-STD", "GOLD-STD", "XAUUSD.PRO", "XAUUSD.RAW")

    # ---- ABSOLUTE risk rails (validator refuses anything looser) ------------
    max_risk_per_trade: float = 0.0075        # 0.75% hard ceiling
    min_risk_per_trade: float = 0.0005        # below this a trade is pointless
    max_daily_loss: float = 0.017             # 1.7% realised+floating => day lock
    max_weekly_drawdown: float = 0.05         # 5% week lock
    cooldown_after_losses: int = 3            # consecutive real losses ...
    loss_cooldown_hours: float = 4.0          # ... pause new entries this long
    max_positions: int = 1                    # one real GOLD position, ever
    max_trades_per_day: int = 12              # runaway protection, not a target
    max_trades_per_session: int = 8
    max_consecutive_losses: int = 999         # base-guard check disabled; the
                                              # V4 cooldown handles streaks
    daily_profit_hard_stop: float = 9.99      # never force/limit a profit day
    emergency_stop: bool = False
    emergency_stop_file: str = "EMERGENCY_STOP.txt"

    # ---- adaptive risk tiers (evidence-based; 0.75% is absolute) ------------
    risk_tier_experimental: Tuple[float, float] = (0.0010, 0.0025)
    risk_tier_moderate: Tuple[float, float] = (0.0025, 0.0045)
    risk_tier_strong: Tuple[float, float] = (0.0045, 0.0060)
    risk_tier_top: Tuple[float, float] = (0.0060, 0.0075)
    tier_moderate_min_n: int = 10             # shadow+real trades needed
    tier_strong_min_n: int = 15
    tier_top_min_n: int = 20
    tier_moderate_min_score: float = 0.10     # ranking score thresholds
    tier_strong_min_score: float = 0.25
    tier_top_min_score: float = 0.40

    # ---- costs & spread ------------------------------------------------------
    slippage_buffer_points: float = 10.0
    commission_per_unit: float = 0.0          # Skilling gold is spread-only
    max_spread_points: float = 60.0
    normal_spread_points: float = 35.0        # above this = "elevated" reducer

    # ---- reward:risk ----------------------------------------------------------
    min_net_rr: float = 2.0                   # after spread/slippage/commission
    preferred_rr: float = 2.5

    # ---- research clock --------------------------------------------------------
    research_days: int = 14
    heartbeat_minutes: int = 5
    debug_logging: bool = True

    # ---- population / learning -------------------------------------------------
    population_seed: int = 20260113           # deterministic strategy seeding
    max_population: int = 40
    min_shadow_trades_for_real: int = 5       # evidence before real money (demo)
    min_regime_trades: int = 3
    shrinkage_k: float = 6.0                  # expectancy shrunk toward 0
    exploration_c: float = 0.30               # UCB exploration constant
    dd_penalty: float = 0.04                  # per R of strategy max drawdown
    instability_penalty: float = 0.02         # per unit of R std-dev
    complexity_penalty: float = 0.01          # per mutation applied
    retire_min_trades: int = 10
    retire_expectancy: float = -0.15          # shrunk R/trade => retired
    bench_recent_n: int = 5
    bench_recent_sum_r: float = -3.0          # last-5 sum R => benched 1 day
    spawn_per_day: int = 3                    # bounded daily variant creation
    spawn_parent_min_n: int = 6
    virtual_equity: float = 10_000.0          # every shadow book starts here
    virtual_risk: float = 0.0035              # same risk basis for all shadows
    shadow_post_watch_bars: int = 60          # stop-too-tight post-mortem (M1)
    adapt_min_n: int = 8                      # evidence before a param adapts
    adapt_rate_threshold: float = 0.40        # e.g. 40% stop-too-tight => widen

    # ---- news protection (manual/schedule-based — same honest model as V3) ---
    news_enabled: bool = True
    news_block_before_min: int = 45
    news_block_after_min: int = 30
    block_nfp: bool = True
    nfp_hour_utc: int = 12
    nfp_minute_utc: int = 30
    block_fomc: bool = True
    block_cpi: bool = True
    block_speeches: bool = True
    fomc_events: Tuple[str, ...] = ()         # "YYYY-MM-DDTHH:MM" UTC — MAINTAIN!
    cpi_events: Tuple[str, ...] = ()
    speech_events: Tuple[str, ...] = ()
    manual_blackouts: Tuple[str, ...] = ()
    require_news_calendar: bool = False       # True = fail closed with no dates

    # ---- sessions (UTC; verify server clock once, set offset) -----------------
    server_utc_offset_hours: int = 0
    asia_enabled: bool = True                 # REAL entries; shadows always learn
    london_enabled: bool = True
    newyork_enabled: bool = True
    asia_start: int = 0
    asia_end: int = 7
    london_start: int = 7
    london_end: int = 16
    ny_start: int = 12
    ny_end: int = 21
    avoid_rollover: Tuple[int, int] = (21, 23)
    friday_last_entry_hour: int = 15
    friday_flat_hour: int = 20
    monday_first_entry_hour: int = 2
    weekend_hold_allowed: bool = False

    # ---- detector parameters (names shared with V3 so its detectors work) ----
    swing_left: int = 2
    swing_right: int = 2
    external_swing_left: int = 3
    external_swing_right: int = 3
    atr_period: int = 14
    displacement_atr_mult: float = 1.2
    displacement_body_ratio: float = 0.55
    bos_use_close: bool = True
    eq_level_atr_tol: float = 0.12
    zone_base_max_candles: int = 5
    zone_base_body_atr: float = 0.60
    zone_max_touches: int = 2
    zone_max_age_candles: int = 400
    fvg_min_size_atr: float = 0.15
    ob_require_structure_link: bool = True
    premium_discount_buffer: float = 0.05

    # ---- data windows (completed candles) -------------------------------------
    window_m1: int = 900
    window_m5: int = 600
    window_m15: int = 450
    window_m30: int = 350
    window_h1: int = 320
    min_candles_required: int = 60

    # ---- persistence ------------------------------------------------------------
    state_dir: str = ""                       # "" = <home>/Documents/XAUUSD_Adaptive_Bot_V4


class V4ConfigValidator:
    """Refuses any configuration that loosens the absolute rails."""

    def __init__(self, cfg: V4Config):
        self.cfg = cfg
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate(self) -> bool:
        c = self.cfg
        err = self.errors.append
        warn = self.warnings.append
        if not 0 < c.max_risk_per_trade <= 0.0075:
            err("max_risk_per_trade must be in (0, 0.0075] — 0.75% is the "
                "absolute ceiling for the 14-day experiment")
        if not 0 < c.max_daily_loss <= 0.017:
            err("max_daily_loss must be in (0, 0.017] — 1.7% combined is the "
                "absolute daily ceiling")
        if not 0 < c.max_weekly_drawdown <= 0.05:
            err("max_weekly_drawdown must be in (0, 0.05]")
        if c.max_positions != 1:
            err("max_positions must be exactly 1 (one real GOLD position)")
        if c.cooldown_after_losses < 1 or c.loss_cooldown_hours <= 0:
            err("loss cooldown must be configured (3 losses default)")
        if c.min_net_rr < 2.0:
            err("min_net_rr below 2.0 violates the experiment's target policy")
        if not (0 < c.min_risk_per_trade < c.max_risk_per_trade):
            err("need 0 < min_risk_per_trade < max_risk_per_trade")
        for name in ("risk_tier_experimental", "risk_tier_moderate",
                     "risk_tier_strong", "risk_tier_top"):
            lo, hi = getattr(c, name)
            if not (0 < lo <= hi <= c.max_risk_per_trade):
                err(f"{name} must fit inside (0, max_risk_per_trade]")
        if c.research_days < 1:
            err("research_days must be >= 1")
        if c.max_population < 8:
            err("max_population too small for meaningful comparison")
        if c.virtual_risk <= 0 or c.virtual_risk > 0.01:
            err("virtual_risk must be in (0, 1%]")
        if c.max_trades_per_day < 1:
            err("max_trades_per_day must be >= 1")
        if c.heartbeat_minutes < 1:
            err("heartbeat_minutes must be >= 1")
        if c.news_enabled and not (c.fomc_events or c.cpi_events):
            warn("NEWS DATES MISSING: no FOMC/CPI dates configured — only the "
                 "NFP first-Friday rule protects you. Add this month's dates "
                 "to fomc_events / cpi_events (or set require_news_calendar "
                 "= True to fail closed).")
        if not c.debug_logging:
            warn("debug_logging is OFF — the research spec asks for it ON")
        return not self.errors

    def report(self) -> str:
        lines = [f"CONFIG ERROR: {e}" for e in self.errors]
        lines += [f"CONFIG WARNING: {w}" for w in self.warnings]
        return "\n".join(lines) if lines else "config OK"


#############################################################################
# V4 - FEATURES
#############################################################################

"""
Per-timeframe feature computation on COMPLETED candles only.

One TFFeatures object is built per timeframe per completed bar and shared
by every strategy that trades that timeframe, so the (comparatively
expensive) structure/liquidity/zone analysis runs once, not per strategy.
All indicator values at index i use candles up to and including i — the
same no-look-ahead discipline as the detectors.
"""


def ema_series(values: Sequence[float], n: int) -> List[float]:
    """EMA; warm-up is a running mean so out[i] is always defined."""
    out: List[float] = []
    if not values:
        return out
    k = 2.0 / (n + 1)
    run = 0.0
    for i, v in enumerate(values):
        if i < n:
            run += v
            out.append(run / (i + 1))
        else:
            out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_series(values: Sequence[float], n: int = 14) -> List[float]:
    """Wilder RSI; neutral 50 during warm-up."""
    out = [50.0] * len(values)
    if len(values) < n + 1:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n
    out[n] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    return out


def highest(candles: Sequence[Candle], n: int, exclude_last: bool = True) -> float:
    """Highest high of the previous n candles (excluding the current one)."""
    src = candles[:-1] if exclude_last else candles
    window = src[-n:] if len(src) >= 1 else []
    return max(c.high for c in window) if window else float("nan")


def lowest(candles: Sequence[Candle], n: int, exclude_last: bool = True) -> float:
    src = candles[:-1] if exclude_last else candles
    window = src[-n:] if len(src) >= 1 else []
    return min(c.low for c in window) if window else float("nan")


@dataclass
class TFFeatures:
    tf: Timeframe
    candles: List[Candle]
    atr: List[float]
    ema20: List[float]
    ema50: List[float]
    ema100: List[float]
    rsi14: List[float]
    structure: StructureState
    zones: List[Zone]
    fvgs: List[FairValueGap]
    order_blocks: List[OrderBlock]
    liquidity: List[LiquidityLevel]
    sweeps: List[SweepEvent]
    eff_ratio: float = 0.0
    roc: float = 0.0
    vol_ma20: float = 0.0
    atr_percentile: float = 0.5
    extras: Dict[str, float] = field(default_factory=dict)

    @property
    def last(self) -> Candle:
        return self.candles[-1]

    @property
    def prev(self) -> Candle:
        return self.candles[-2]

    @property
    def atr_now(self) -> float:
        return self.atr[-1] if self.atr else 0.0

    @property
    def close(self) -> float:
        return self.candles[-1].close


class FeatureBuilder:

    def __init__(self, cfg):
        """cfg: V4Config (carries the detector parameters)."""
        self.cfg = cfg

    def build(self, tf: Timeframe, candles: List[Candle],
              session_marks: Optional[Dict[str, float]] = None) -> Optional[TFFeatures]:
        cfg = self.cfg
        if len(candles) < max(cfg.min_candles_required, cfg.atr_period + 25):
            return None
        closes = [c.close for c in candles]
        vols = [c.volume for c in candles]
        atr = atr_series(candles, cfg.atr_period)
        analyzer = StructureAnalyzer(cfg)
        structure = analyzer.analyze(candles, atr)
        zones = SupplyDemandDetector(cfg, tf).detect(candles, structure)
        fvgs = FVGDetector(cfg, tf).detect(candles)
        liq = LiquidityDetector(cfg)
        levels = liq.detect_levels(candles, structure.swings, session_marks)
        sweeps = liq.update_states(levels, candles, atr)
        obs = OrderBlockDetector(cfg, tf).detect(candles, structure, sweeps)
        hist = [a for a in atr[-200:] if a > 0]
        atr_pct = (sum(1 for a in hist if a <= atr[-1]) / len(hist)) if hist else 0.5
        return TFFeatures(
            tf=tf, candles=candles, atr=atr,
            ema20=ema_series(closes, 20), ema50=ema_series(closes, 50),
            ema100=ema_series(closes, 100), rsi14=rsi_series(closes, 14),
            structure=structure, zones=zones, fvgs=fvgs, order_blocks=obs,
            liquidity=levels, sweeps=sweeps,
            eff_ratio=efficiency_ratio(candles, 20),
            roc=rate_of_change(candles, 20),
            vol_ma20=(sum(vols[-20:]) / 20.0) if len(vols) >= 20 else 0.0,
            atr_percentile=atr_pct,
        )


#############################################################################
# V4 - STRATEGY SPACE (8 ARCHETYPES)
#############################################################################

"""
Autonomous strategy space.

A strategy is a CONFIGURATION, not code: an archetype (one of eight exact
rule templates below) plus a timeframe plus bounded numeric parameters,
management rules, a regime whitelist and a session whitelist.  Every
strategy therefore has exact, reproducible rules for entry, stop, target,
invalidation, regime and management, and is stored with a unique id and
version.  The learning system may create bounded parameter variants
("mutations") of successful strategies and retire failing ones — it can
never invent rules outside these templates and never touches source code.

Archetypes (long and short are symmetric):
  TREND_PULLBACK   trend (structure + EMA100) + pullback to EMA + resume
  DONCHIAN_BREAK   N-bar channel breakout + range expansion + volume
  SWEEP_REVERSAL   liquidity sweep + reclaim (+ optional displacement)
  RANGE_FADE       dealing-range edge + RSI extreme + rejection candle
  MOMENTUM_CONT    consecutive displacement + micro-pause continuation
  FVG_RETEST       fresh fair-value-gap retest that holds, with trend
  SESSION_OPEN     London/NY open range break with displacement
  SD_ZONE          fresh supply/demand zone reaction with rejection close

Every signal must clear the net reward-to-risk floor (>= 2.0R after
spread/slippage/commission) and targets blocked by major opposing
structure are clipped or rejected.
"""


TREND_REGIMES = (Regime.STRONG_BULL.value, Regime.WEAK_BULL.value,
                 Regime.STRONG_BEAR.value, Regime.WEAK_BEAR.value)
ALL_TRADEABLE = TREND_REGIMES + (Regime.RANGE.value, Regime.EXPANSION.value,
                                 Regime.COMPRESSION.value,
                                 Regime.REVERSAL_ATTEMPT.value)

MGMT_MODES = ("FULL_TP", "BE_1R", "PARTIAL_RUNNER", "ATR_TRAIL",
              "STRUCT_TRAIL")

TARGET_MODES = ("RR", "LIQUIDITY", "ATR")


@dataclass
class StrategyConfig:
    sid: str
    archetype: str
    version: int
    tf: str                                   # "M5" | "M15" | "M30" | "H1"
    params: Dict[str, float]
    regimes: Tuple[str, ...]
    sessions: Tuple[str, ...]                 # ("ANY",) or session names
    mgmt: Dict[str, float]                    # mode index + thresholds
    status: str = "active"                    # active | benched | retired
    bench_until: str = ""                     # ISO date
    parent: str = ""
    mutations: int = 0
    created: str = ""

    @property
    def mgmt_mode(self) -> str:
        return MGMT_MODES[int(self.mgmt.get("mode", 0)) % len(MGMT_MODES)]

    def to_dict(self) -> dict:
        return {"sid": self.sid, "archetype": self.archetype,
                "version": self.version, "tf": self.tf,
                "params": dict(self.params), "regimes": list(self.regimes),
                "sessions": list(self.sessions), "mgmt": dict(self.mgmt),
                "status": self.status, "bench_until": self.bench_until,
                "parent": self.parent, "mutations": self.mutations,
                "created": self.created}

    @staticmethod
    def from_dict(d: dict) -> "StrategyConfig":
        return StrategyConfig(
            sid=d["sid"], archetype=d["archetype"], version=d["version"],
            tf=d["tf"], params=dict(d["params"]),
            regimes=tuple(d["regimes"]), sessions=tuple(d["sessions"]),
            mgmt=dict(d["mgmt"]), status=d.get("status", "active"),
            bench_until=d.get("bench_until", ""), parent=d.get("parent", ""),
            mutations=d.get("mutations", 0), created=d.get("created", ""))


@dataclass
class Signal:
    strategy_id: str
    tf: str
    direction: Direction
    entry_ref: float                 # decision-bar close (fills are later)
    stop: float
    target: float
    reason: str
    confluence: int
    created: datetime
    invalidation: str = "close beyond stop level before entry"

    def rr(self, entry: Optional[float] = None) -> float:
        e = entry if entry is not None else self.entry_ref
        risk = abs(e - self.stop)
        return abs(self.target - e) / risk if risk > 0 else 0.0


@dataclass
class EvalContext:
    regime: str
    session: str
    spread_points: float
    point: float
    now: datetime
    cost: float                      # price-units round-trip cost estimate
    min_net_rr: float
    asian_range: Optional[Tuple[float, float]] = None
    minutes_into_london: Optional[int] = None
    minutes_into_ny: Optional[int] = None


# ---------------------------------------------------------------------------
# parameter bounds (mutations may never leave these boxes)
# ---------------------------------------------------------------------------

PARAM_BOUNDS: Dict[str, Dict[str, Tuple[float, float]]] = {
    "TREND_PULLBACK": {"ema_sel": (0, 1), "swing_lookback": (5, 14),
                       "buffer_atr": (0.2, 0.6), "rr": (2.0, 3.5),
                       "target_mode": (0, 2), "atr_target": (2.5, 4.5),
                       "rsi_floor": (45, 55)},
    "DONCHIAN_BREAK": {"ch_len": (20, 55), "exp_mult": (1.1, 1.7),
                       "vol_mult": (1.0, 2.0), "stop_atr": (1.2, 2.0),
                       "rr": (2.0, 3.5), "target_mode": (0, 2),
                       "atr_target": (2.0, 4.0)},
    "SWEEP_REVERSAL": {"recency": (3, 10), "disp_req": (0, 1),
                       "buffer_atr": (0.2, 0.6), "rr": (2.0, 3.0),
                       "target_mode": (0, 2), "atr_target": (2.0, 3.5)},
    "RANGE_FADE": {"edge_frac": (0.10, 0.25), "rsi_os": (25, 35),
                   "wick_frac": (0.40, 0.60), "buffer_atr": (0.2, 0.6),
                   "target_sel": (0, 1), "rr": (2.0, 3.0)},
    "MOMENTUM_CONT": {"mom_count": (2, 3), "pause_frac": (0.4, 0.7),
                      "stop_atr": (1.0, 1.8), "atr_target": (2.5, 4.5),
                      "rr": (2.0, 3.5), "target_mode": (0, 2)},
    "FVG_RETEST": {"recency": (4, 15), "disp_req": (0, 1),
                   "buffer_atr": (0.2, 0.6), "rr": (2.0, 3.5),
                   "target_mode": (0, 2), "atr_target": (2.0, 4.0)},
    "SESSION_OPEN": {"open_window": (15, 60), "range_mult": (1.0, 2.0),
                     "stop_sel": (0, 1), "rr": (2.0, 3.0),
                     "pre_range_bars": (12, 36), "target_mode": (0, 2),
                     "atr_target": (2.0, 3.5)},
    "SD_ZONE": {"min_quality": (0.40, 0.70), "buffer_atr": (0.2, 0.6),
                "trend_req": (0, 1), "rr": (2.0, 3.5),
                "target_mode": (0, 2), "atr_target": (2.0, 4.0)},
}

MGMT_BOUNDS = {"mode": (0, len(MGMT_MODES) - 1), "be_r": (0.8, 2.0),
               "partial_r": (1.5, 2.0), "partial_frac": (0.4, 0.6),
               "trail_atr": (1.5, 2.5)}


def _clamp(name_bounds, key, value):
    lo, hi = name_bounds[key]
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# shared building blocks
# ---------------------------------------------------------------------------

def _swing_stop(direction: Direction, f: TFFeatures, lookback: int,
                buffer_atr: float) -> float:
    if direction == Direction.LONG:
        return lowest(f.candles, lookback, exclude_last=False) \
            - buffer_atr * f.atr_now
    return highest(f.candles, lookback, exclude_last=False) \
        + buffer_atr * f.atr_now


def _blocking_zone_edge(direction: Direction, f: TFFeatures,
                        entry: float) -> Optional[float]:
    """Nearest strong opposing zone edge in the trade direction."""
    kind = ZoneKind.SUPPLY if direction == Direction.LONG else ZoneKind.DEMAND
    best = None
    for z in SupplyDemandDetector.active_zones(f.zones, kind,
                                               min_quality=0.45):
        edge = z.lower if direction == Direction.LONG else z.upper
        if direction == Direction.LONG and edge > entry:
            best = edge if best is None else min(best, edge)
        if direction == Direction.SHORT and edge < entry:
            best = edge if best is None else max(best, edge)
    return best


def _target(direction: Direction, entry: float, stop: float, f: TFFeatures,
            mode_idx: float, rr: float, atr_mult: float, ctx: EvalContext
            ) -> Optional[Tuple[float, str]]:
    """Target by mode; rejects/clips targets blocked by opposing structure
    and rejects anything below the net-RR floor."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = direction.sign
    mode = TARGET_MODES[int(mode_idx) % len(TARGET_MODES)]
    if mode == "RR":
        tgt, why = entry + sign * rr * risk, f"{rr:.1f}R multiple"
    elif mode == "ATR":
        tgt, why = entry + sign * atr_mult * f.atr_now, \
            f"{atr_mult:.1f}x ATR projection"
    else:
        pools = LiquidityDetector.targets_beyond(f.liquidity, entry,
                                                 direction, count=3)
        tgt = None
        why = ""
        for lvl in pools:
            net = (abs(lvl.price - entry) - ctx.cost) / (risk + ctx.cost)
            if net >= ctx.min_net_rr:
                tgt, why = lvl.price, f"opposing {lvl.kind.value} liquidity"
                break
        if tgt is None:
            return None
    # opposing-structure block: clip to the zone edge; reject if too near
    block = _blocking_zone_edge(direction, f, entry)
    if block is not None and (block - tgt) * sign < 0:
        tgt, why = block, why + " (clipped at opposing zone)"
    net_rr = (abs(tgt - entry) - ctx.cost) / (risk + ctx.cost)
    if net_rr < ctx.min_net_rr:
        return None
    return tgt, why


def _stop_sanity(direction: Direction, entry: float, stop: float,
                 f: TFFeatures) -> bool:
    dist = abs(entry - stop)
    if dist <= 0 or f.atr_now <= 0:
        return False
    return 0.30 * f.atr_now <= dist <= 4.0 * f.atr_now


def _make(scfg: "StrategyConfig", f: TFFeatures, ctx: EvalContext,
          direction: Direction, stop: float, reason: str,
          confluence: int) -> Optional[Signal]:
    entry = f.close
    p = scfg.params
    if not _stop_sanity(direction, entry, stop, f):
        return None
    t = _target(direction, entry, stop, f, p.get("target_mode", 0),
                p.get("rr", 2.5), p.get("atr_target", 3.0), ctx)
    if t is None:
        return None
    target, twhy = t
    return Signal(strategy_id=scfg.sid, tf=scfg.tf, direction=direction,
                  entry_ref=entry, stop=stop, target=target,
                  reason=f"{reason}; target {twhy}",
                  confluence=confluence, created=ctx.now)


# ---------------------------------------------------------------------------
# archetype rules (exact and reproducible; long/short symmetric)
# ---------------------------------------------------------------------------

def _eval_trend_pullback(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    ema_p = f.ema20 if int(p.get("ema_sel", 0)) == 0 else f.ema50
    last, prev = f.last, f.prev
    trend = f.structure.trend.value
    if trend == "BULLISH" and f.close > f.ema100[-1]:
        touched = prev.low <= ema_p[-2] or last.low <= ema_p[-1]
        resumed = last.bullish and last.close > ema_p[-1]
        if touched and resumed and f.rsi14[-1] >= p.get("rsi_floor", 50):
            stop = _swing_stop(Direction.LONG, f,
                               int(p.get("swing_lookback", 8)),
                               p.get("buffer_atr", 0.35))
            return _make(scfg, f, ctx, Direction.LONG, stop,
                         "bull trend pullback to EMA resumed", 3)
    if trend == "BEARISH" and f.close < f.ema100[-1]:
        touched = prev.high >= ema_p[-2] or last.high >= ema_p[-1]
        resumed = last.bearish and last.close < ema_p[-1]
        if touched and resumed and f.rsi14[-1] <= 100 - p.get("rsi_floor", 50):
            stop = _swing_stop(Direction.SHORT, f,
                               int(p.get("swing_lookback", 8)),
                               p.get("buffer_atr", 0.35))
            return _make(scfg, f, ctx, Direction.SHORT, stop,
                         "bear trend pullback to EMA resumed", 3)
    return None


def _eval_donchian_break(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    n = int(p.get("ch_len", 34))
    if len(f.candles) < n + 2 or f.atr_now <= 0:
        return None
    last = f.last
    hi, lo = highest(f.candles, n), lowest(f.candles, n)
    expanded = last.range >= p.get("exp_mult", 1.3) * f.atr_now
    vol_ok = f.vol_ma20 <= 0 or last.volume >= p.get("vol_mult", 1.2) * f.vol_ma20
    if not (expanded and vol_ok):
        return None
    stop_atr = p.get("stop_atr", 1.5)
    if last.close > hi and last.bullish:
        stop = last.close - stop_atr * f.atr_now
        return _make(scfg, f, ctx, Direction.LONG, stop,
                     f"close above {n}-bar high with expansion+volume", 3)
    if last.close < lo and last.bearish:
        stop = last.close + stop_atr * f.atr_now
        return _make(scfg, f, ctx, Direction.SHORT, stop,
                     f"close below {n}-bar low with expansion+volume", 3)
    return None


def _eval_sweep_reversal(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    recency = int(p.get("recency", 6))
    n = len(f.candles)
    for sv in reversed(f.sweeps):
        if sv.index < n - recency:
            break
        if not sv.valid:
            continue
        if int(p.get("disp_req", 0)) == 1 and not sv.displaced_away:
            continue
        if not sv.level.buy_side:            # sell-side swept -> long
            if f.last.close > sv.level.price:
                stop = sv.extreme - p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.LONG, stop,
                             f"sweep of {sv.level.kind.value} reclaimed",
                             2 + int(sv.displaced_away))
        else:                                 # buy-side swept -> short
            if f.last.close < sv.level.price:
                stop = sv.extreme + p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.SHORT, stop,
                             f"sweep of {sv.level.kind.value} reclaimed",
                             2 + int(sv.displaced_away))
    return None


def _eval_range_fade(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    rng = f.structure.dealing_range
    if rng is None or f.atr_now <= 0:
        return None
    pos = rng.position_of(f.close)
    edge = p.get("edge_frac", 0.18)
    last = f.last
    if pos <= edge and f.rsi14[-1] <= p.get("rsi_os", 30):
        rejected = last.range > 0 and last.lower_wick >= \
            p.get("wick_frac", 0.5) * last.range and last.close >= last.open
        if rejected:
            stop = rng.low - p.get("buffer_atr", 0.35) * f.atr_now
            tgt_price = rng.low + (0.5 if int(p.get("target_sel", 0)) == 0
                                   else 1.0 - edge) * (rng.high - rng.low)
            risk = abs(f.close - stop)
            net = (abs(tgt_price - f.close) - ctx.cost) / (risk + ctx.cost) \
                if risk > 0 else 0
            if net >= ctx.min_net_rr and _stop_sanity(Direction.LONG,
                                                      f.close, stop, f):
                return Signal(scfg.sid, scfg.tf, Direction.LONG, f.close,
                              stop, tgt_price,
                              "range-low fade: RSI oversold + rejection; "
                              "target range level", 3, ctx.now)
    if pos >= 1.0 - edge and f.rsi14[-1] >= 100 - p.get("rsi_os", 30):
        rejected = last.range > 0 and last.upper_wick >= \
            p.get("wick_frac", 0.5) * last.range and last.close <= last.open
        if rejected:
            stop = rng.high + p.get("buffer_atr", 0.35) * f.atr_now
            tgt_price = rng.high - (0.5 if int(p.get("target_sel", 0)) == 0
                                    else 1.0 - edge) * (rng.high - rng.low)
            risk = abs(f.close - stop)
            net = (abs(f.close - tgt_price) - ctx.cost) / (risk + ctx.cost) \
                if risk > 0 else 0
            if net >= ctx.min_net_rr and _stop_sanity(Direction.SHORT,
                                                      f.close, stop, f):
                return Signal(scfg.sid, scfg.tf, Direction.SHORT, f.close,
                              stop, tgt_price,
                              "range-high fade: RSI overbought + rejection; "
                              "target range level", 3, ctx.now)
    return None


def _eval_momentum_cont(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    need = int(p.get("mom_count", 2))
    if len(f.candles) < need + 2 or f.atr_now <= 0:
        return None
    last = f.last
    if last.body > p.get("pause_frac", 0.5) * f.atr_now:
        return None                       # need the micro-pause candle
    push = f.candles[-(need + 1):-1]
    disp = [c for c in push
            if c.body >= 1.0 * f.atr_now and c.body_ratio >= 0.5]
    if len(disp) < need:
        return None
    bullish = all(c.bullish for c in disp)
    bearish = all(c.bearish for c in disp)
    stop_atr = p.get("stop_atr", 1.3)
    if bullish:
        stop = min(last.low, f.close - stop_atr * f.atr_now)
        return _make(scfg, f, ctx, Direction.LONG, stop,
                     f"{need} displacement candles up + pause", 2)
    if bearish:
        stop = max(last.high, f.close + stop_atr * f.atr_now)
        return _make(scfg, f, ctx, Direction.SHORT, stop,
                     f"{need} displacement candles down + pause", 2)
    return None


def _eval_fvg_retest(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    recency = int(p.get("recency", 8))
    n = len(f.candles)
    last = f.last
    for g in reversed(f.fvgs):
        if g.created_index < n - recency:
            break
        if g.state.value == "MITIGATED":
            continue
        if int(p.get("disp_req", 1)) == 1 and not g.from_displacement:
            continue
        if g.direction == Direction.LONG and f.close > f.ema50[-1]:
            tapped = last.low <= g.upper
            held = last.close > g.upper and last.bullish
            if tapped and held:
                stop = g.lower - p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.LONG, stop,
                             "bullish FVG retested and held", 3)
        if g.direction == Direction.SHORT and f.close < f.ema50[-1]:
            tapped = last.high >= g.lower
            held = last.close < g.lower and last.bearish
            if tapped and held:
                stop = g.upper + p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.SHORT, stop,
                             "bearish FVG retested and held", 3)
    return None


def _eval_session_open(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    window = int(p.get("open_window", 40))
    is_london = "LONDON" in scfg.sessions
    minutes = ctx.minutes_into_london if is_london else ctx.minutes_into_ny
    if minutes is None or not (0 <= minutes <= window):
        return None
    if is_london and ctx.asian_range is not None:
        lo, hi = ctx.asian_range
    else:
        bars = int(p.get("pre_range_bars", 24))
        hi, lo = highest(f.candles, bars), lowest(f.candles, bars)
    if not (hi > lo > 0):
        return None
    last = f.last
    disp = last.body >= 1.0 * f.atr_now and last.body_ratio >= 0.5
    rng_size = hi - lo
    if not disp or rng_size <= 0:
        return None
    stop_sel = int(p.get("stop_sel", 0))
    if last.close > hi and last.bullish:
        stop = (lo if stop_sel == 0 else (hi + lo) / 2.0) \
            - 0.15 * f.atr_now
        return _make(scfg, f, ctx, Direction.LONG, stop,
                     f"session-open break above pre-range ({rng_size:.2f})", 2)
    if last.close < lo and last.bearish:
        stop = (hi if stop_sel == 0 else (hi + lo) / 2.0) \
            + 0.15 * f.atr_now
        return _make(scfg, f, ctx, Direction.SHORT, stop,
                     f"session-open break below pre-range ({rng_size:.2f})", 2)
    return None


def _eval_sd_zone(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    q = p.get("min_quality", 0.5)
    last = f.last
    trend = f.structure.trend.value
    for z in SupplyDemandDetector.active_zones(f.zones, None, min_quality=q):
        if z.kind == ZoneKind.DEMAND:
            if int(p.get("trend_req", 0)) == 1 and trend == "BEARISH":
                continue
            tapped = last.low <= z.upper and last.high >= z.lower
            held = last.close > z.upper and last.bullish
            if tapped and held:
                stop = z.lower - p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.LONG, stop,
                             f"demand zone {z.pattern.value} reaction "
                             f"(fresh {z.freshness:.2f})", 3)
        else:
            if int(p.get("trend_req", 0)) == 1 and trend == "BULLISH":
                continue
            tapped = last.high >= z.lower and last.low <= z.upper
            held = last.close < z.lower and last.bearish
            if tapped and held:
                stop = z.upper + p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.SHORT, stop,
                             f"supply zone {z.pattern.value} reaction "
                             f"(fresh {z.freshness:.2f})", 3)
    return None


ARCHETYPE_EVAL = {
    "TREND_PULLBACK": _eval_trend_pullback,
    "DONCHIAN_BREAK": _eval_donchian_break,
    "SWEEP_REVERSAL": _eval_sweep_reversal,
    "RANGE_FADE": _eval_range_fade,
    "MOMENTUM_CONT": _eval_momentum_cont,
    "FVG_RETEST": _eval_fvg_retest,
    "SESSION_OPEN": _eval_session_open,
    "SD_ZONE": _eval_sd_zone,
}

ARCHETYPE_REGIMES = {
    "TREND_PULLBACK": TREND_REGIMES + (Regime.EXPANSION.value,),
    "DONCHIAN_BREAK": (Regime.STRONG_BULL.value, Regime.STRONG_BEAR.value,
                       Regime.EXPANSION.value, Regime.COMPRESSION.value),
    "SWEEP_REVERSAL": (Regime.RANGE.value, Regime.REVERSAL_ATTEMPT.value,
                       Regime.WEAK_BULL.value, Regime.WEAK_BEAR.value),
    "RANGE_FADE": (Regime.RANGE.value, Regime.COMPRESSION.value),
    "MOMENTUM_CONT": (Regime.STRONG_BULL.value, Regime.STRONG_BEAR.value,
                      Regime.EXPANSION.value),
    "FVG_RETEST": TREND_REGIMES + (Regime.EXPANSION.value,
                                   Regime.REVERSAL_ATTEMPT.value),
    "SESSION_OPEN": ALL_TRADEABLE,
    "SD_ZONE": TREND_REGIMES + (Regime.RANGE.value,),
}


def evaluate_strategy(scfg: StrategyConfig, f: TFFeatures,
                      ctx: EvalContext) -> Optional[Signal]:
    """Run one strategy's exact rules against fresh features.  Regime and
    session whitelists are enforced here; the caller enforces global
    safety gates (news/spread/locks) separately."""
    if scfg.status != "active":
        return None
    if ctx.regime not in scfg.regimes:
        return None
    if "ANY" not in scfg.sessions and ctx.session not in scfg.sessions:
        return None
    fn = ARCHETYPE_EVAL.get(scfg.archetype)
    if fn is None or f is None:
        return None
    return fn(scfg, f, ctx)


# ---------------------------------------------------------------------------
# population: deterministic seeding + bounded mutation
# ---------------------------------------------------------------------------

def _mgmt(mode_idx: int) -> Dict[str, float]:
    return {"mode": float(mode_idx % len(MGMT_MODES)), "be_r": 1.2,
            "partial_r": 1.8, "partial_frac": 0.5, "trail_atr": 2.0}


def seed_population(created_iso: str) -> List[StrategyConfig]:
    """Deterministic, diverse initial population across archetypes,
    timeframes, target modes and management styles."""
    seeds: List[StrategyConfig] = []
    k = 0

    def add(arch, tf, params, sessions=("ANY",), mgmt_idx=None):
        nonlocal k
        k += 1
        m = _mgmt(mgmt_idx if mgmt_idx is not None else k)
        seeds.append(StrategyConfig(
            sid=f"{arch}-{tf}-{k:02d}", archetype=arch, version=1, tf=tf,
            params=params, regimes=ARCHETYPE_REGIMES[arch],
            sessions=sessions, mgmt=m, created=created_iso))

    for tf in ("M5", "M15", "M30"):
        add("TREND_PULLBACK", tf,
            {"ema_sel": 0, "swing_lookback": 8, "buffer_atr": 0.35,
             "rr": 2.5, "target_mode": 0, "atr_target": 3.0,
             "rsi_floor": 50})
    add("TREND_PULLBACK", "M15",
        {"ema_sel": 1, "swing_lookback": 12, "buffer_atr": 0.45,
         "rr": 3.0, "target_mode": 1, "atr_target": 3.5, "rsi_floor": 48})
    for tf in ("M5", "M15", "H1"):
        add("DONCHIAN_BREAK", tf,
            {"ch_len": 34, "exp_mult": 1.3, "vol_mult": 1.2,
             "stop_atr": 1.5, "rr": 2.5, "target_mode": 2,
             "atr_target": 3.0})
    add("DONCHIAN_BREAK", "M15",
        {"ch_len": 55, "exp_mult": 1.2, "vol_mult": 1.0, "stop_atr": 1.8,
         "rr": 3.0, "target_mode": 0, "atr_target": 3.5})
    for tf in ("M5", "M15"):
        add("SWEEP_REVERSAL", tf,
            {"recency": 6, "disp_req": 0, "buffer_atr": 0.35, "rr": 2.5,
             "target_mode": 1, "atr_target": 2.5})
    add("SWEEP_REVERSAL", "M15",
        {"recency": 8, "disp_req": 1, "buffer_atr": 0.45, "rr": 2.0,
         "target_mode": 0, "atr_target": 3.0})
    for tf in ("M5", "M15"):
        add("RANGE_FADE", tf,
            {"edge_frac": 0.18, "rsi_os": 30, "wick_frac": 0.5,
             "buffer_atr": 0.35, "target_sel": 0, "rr": 2.0,
             "target_mode": 0, "atr_target": 2.5})
    for tf in ("M5", "M15"):
        add("MOMENTUM_CONT", tf,
            {"mom_count": 2, "pause_frac": 0.5, "stop_atr": 1.3,
             "atr_target": 3.0, "rr": 2.5, "target_mode": 2})
    for tf in ("M5", "M15"):
        add("FVG_RETEST", tf,
            {"recency": 8, "disp_req": 1, "buffer_atr": 0.35, "rr": 2.5,
             "target_mode": 0, "atr_target": 3.0})
    add("SESSION_OPEN", "M5",
        {"open_window": 40, "range_mult": 1.5, "stop_sel": 0, "rr": 2.0,
         "pre_range_bars": 24, "target_mode": 0, "atr_target": 2.5},
        sessions=("LONDON", "OVERLAP"))
    add("SESSION_OPEN", "M5",
        {"open_window": 40, "range_mult": 1.5, "stop_sel": 1, "rr": 2.0,
         "pre_range_bars": 24, "target_mode": 2, "atr_target": 2.5},
        sessions=("NEW_YORK", "OVERLAP"))
    for tf in ("M15", "M30"):
        add("SD_ZONE", tf,
            {"min_quality": 0.5, "buffer_atr": 0.35, "trend_req": 1,
             "rr": 2.5, "target_mode": 1, "atr_target": 3.0})
    return seeds


def mutate_strategy(parent: StrategyConfig, rng: random.Random,
                    serial: int, created_iso: str) -> StrategyConfig:
    """Bounded variant: perturb 1-2 numeric parameters (and occasionally
    the management style) inside PARAM_BOUNDS.  Never changes the
    archetype rules themselves."""
    bounds = PARAM_BOUNDS[parent.archetype]
    params = dict(parent.params)
    keys = [key for key in params if key in bounds]
    rng.shuffle(keys)
    for key in keys[:rng.randint(1, 2)]:
        lo, hi = bounds[key]
        span = hi - lo
        params[key] = _clamp(bounds, key,
                             params[key] + rng.uniform(-0.25, 0.25) * span)
        if float(params[key]).is_integer() or key in ("ch_len", "recency",
                                                      "swing_lookback",
                                                      "mom_count",
                                                      "pre_range_bars",
                                                      "open_window",
                                                      "rsi_os", "rsi_floor"):
            params[key] = float(int(round(params[key])))
    mgmt = dict(parent.mgmt)
    if rng.random() < 0.30:
        mgmt["mode"] = float(rng.randint(0, len(MGMT_MODES) - 1))
    for key in ("be_r", "partial_r", "partial_frac", "trail_atr"):
        if rng.random() < 0.20:
            lo, hi = MGMT_BOUNDS[key]
            mgmt[key] = max(lo, min(hi, mgmt.get(key, lo)
                                    + rng.uniform(-0.15, 0.15) * (hi - lo)))
    base = parent.sid.split("-m")[0]
    return StrategyConfig(
        sid=f"{base}-m{serial:02d}", archetype=parent.archetype,
        version=parent.version + 1, tf=parent.tf, params=params,
        regimes=parent.regimes, sessions=parent.sessions, mgmt=mgmt,
        parent=parent.sid, mutations=parent.mutations + 1,
        created=created_iso)


#############################################################################
# V4 - SHADOW PORTFOLIOS
#############################################################################

"""
Shadow (virtual) portfolios — one per strategy, running in parallel.

Every strategy receives the same completed market data and records
hypothetical trades against its own virtual account (same starting equity
and the same fixed virtual risk fraction for every strategy, so books are
directly comparable; ranking itself is done in R units).

Realism rules:
  * a signal formed at a decision-bar close is FILLED at the OPEN of the
    next completed M1 candle, plus spread (buys) and a slippage allowance
    — never on the signal bar itself (no look-ahead);
  * exits are resolved on completed M1 candles; when a candle spans both
    stop and target, the STOP is assumed to hit first (conservative);
  * spread is charged on the fill side, commission per unit both ways;
  * MFE/MAE are tracked from M1 extremes in R units;
  * after a stop-out the engine watches the next N bars to record whether
    the original target would still have been reached ("stop too tight"
    evidence for the learning system);
  * one open virtual trade per strategy at a time.
"""


@dataclass
class VirtualTrade:
    trade_id: str
    strategy_id: str
    tf: str
    direction: Direction
    signal_time: datetime
    entry_time: Optional[datetime]
    entry: float
    stop: float
    initial_stop: float
    target: float
    units: float
    risk_money: float
    risk_pct: float
    spread_points: float
    regime: str
    session: str
    reason: str
    mgmt_mode: str
    status: str = "pending"           # pending | open | closed
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    profit: float = 0.0
    r_multiple: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    be_done: bool = False
    partial_done: bool = False
    partial_profit: float = 0.0
    bars_open: int = 0
    # post-stop watch (stop-too-tight evidence)
    watch_bars_left: int = 0
    watch_target_hit: bool = False

    def risk_dist(self) -> float:
        return abs(self.entry - self.initial_stop)


class ShadowEngine:
    """Runs every strategy's virtual book against completed M1 candles."""

    def __init__(self, cfg, log: Callable[[str], None],
                 on_close: Callable[[VirtualTrade], None],
                 record_row: Callable[[VirtualTrade], None],
                 on_watch_done: Optional[Callable[[VirtualTrade], None]] = None):
        self.cfg = cfg
        self.log = log
        self.on_close = on_close          # -> learning system
        self.record_row = record_row      # -> CSV persistence
        self.on_watch_done = on_watch_done  # post-stop watch resolution
        self.open: Dict[str, VirtualTrade] = {}       # strategy_id -> trade
        self.pending: List[VirtualTrade] = []
        self.watching: List[VirtualTrade] = []
        self.equity: Dict[str, float] = {}            # strategy_id -> equity
        self._serial = 0

    # ------------------------------------------------------------ intake
    def submit(self, scfg: StrategyConfig, sig: Signal, spread_points: float,
               point: float, regime: str, session: str) -> bool:
        """Queue a signal for fill at the next completed M1 open.
        One virtual position per strategy; duplicates are refused."""
        sid = scfg.sid
        if sid in self.open or any(t.strategy_id == sid for t in self.pending):
            return False
        eq = self.equity.setdefault(sid, self.cfg.virtual_equity)
        risk_money = eq * self.cfg.virtual_risk
        risk_dist = abs(sig.entry_ref - sig.stop)
        if risk_dist <= 0 or eq <= 0:
            return False
        cost_per_unit = (spread_points + self.cfg.slippage_buffer_points) \
            * point + 2 * self.cfg.commission_per_unit
        units = risk_money / (risk_dist + cost_per_unit)
        if units <= 0:
            return False
        self._serial += 1
        self.pending.append(VirtualTrade(
            trade_id=f"sh{self._serial:06d}", strategy_id=sid, tf=sig.tf,
            direction=sig.direction, signal_time=sig.created,
            entry_time=None, entry=sig.entry_ref, stop=sig.stop,
            initial_stop=sig.stop, target=sig.target, units=units,
            risk_money=risk_money, risk_pct=self.cfg.virtual_risk,
            spread_points=spread_points, regime=regime, session=session,
            reason=sig.reason, mgmt_mode=scfg.mgmt_mode))
        return True

    # ------------------------------------------------------------ engine
    def on_m1(self, candle: Candle, spread_points: float, point: float,
              now: datetime, mgmt_lookup: Dict[str, dict],
              tf_atr: Dict[str, float]) -> None:
        """Advance every book by one completed M1 candle.
        mgmt_lookup: strategy_id -> mgmt param dict (be_r/partial_r/...).
        tf_atr: strategy tf -> current ATR (for trailing)."""
        self._fill_pending(candle, spread_points, point, now)
        for sid in list(self.open.keys()):
            self._manage(self.open[sid], candle, spread_points, point, now,
                         mgmt_lookup.get(sid, {}), tf_atr)
        self._advance_watch(candle)

    def _fill_pending(self, candle: Candle, spread_points: float,
                      point: float, now: datetime) -> None:
        still: List[VirtualTrade] = []
        for t in self.pending:
            if candle.time <= t.signal_time:
                still.append(t)           # candle not after the signal yet
                continue
            slip = self.cfg.slippage_buffer_points * point
            if t.direction == Direction.LONG:
                fill = candle.open + spread_points * point + slip
            else:
                fill = candle.open - slip
            # keep the planned risk honest: recompute from actual fill
            t.entry = fill
            t.entry_time = candle.time
            t.status = "open"
            if abs(fill - t.initial_stop) <= 0:
                t.status = "closed"
                t.exit_reason = "DEGENERATE_FILL"
                continue
            self.open[t.strategy_id] = t
        self.pending = still

    def _manage(self, t: VirtualTrade, c: Candle, spread_points: float,
                point: float, now: datetime, mgmt: dict,
                tf_atr: Dict[str, float]) -> None:
        d = 1 if t.direction == Direction.LONG else -1
        risk = t.risk_dist()
        if risk <= 0:
            return
        t.bars_open += 1
        spread_px = spread_points * point

        # excursions in R (bid-based candles; conservative on the far side)
        if d > 0:
            t.mfe_r = max(t.mfe_r, (c.high - t.entry) / risk)
            t.mae_r = max(t.mae_r, (t.entry - c.low) / risk)
        else:
            t.mfe_r = max(t.mfe_r, (t.entry - c.low) / risk)
            t.mae_r = max(t.mae_r, (c.high + spread_px - t.entry) / risk)

        # exit checks: STOP FIRST (conservative)
        stop_hit = (c.low <= t.stop) if d > 0 else (c.high + spread_px >= t.stop)
        tgt_hit = (c.high >= t.target + spread_px) if d > 0 \
            else (c.low <= t.target)
        if stop_hit:
            self._close(t, t.stop, "STOP_LOSS", now)
            return
        if tgt_hit:
            self._close(t, t.target, "TAKE_PROFIT", now)
            return

        # management by the strategy's configured mode
        r_now = (c.close - t.entry) * d / risk
        mode = t.mgmt_mode
        be_r = mgmt.get("be_r", 1.2)
        if mode in ("BE_1R", "PARTIAL_RUNNER", "ATR_TRAIL", "STRUCT_TRAIL") \
                and not t.be_done and r_now >= be_r:
            be = t.entry + d * (spread_px + self.cfg.slippage_buffer_points
                                * point)
            if (be - t.stop) * d > 0:
                t.stop = be
            t.be_done = True
        if mode == "PARTIAL_RUNNER" and not t.partial_done \
                and r_now >= mgmt.get("partial_r", 1.8):
            frac = mgmt.get("partial_frac", 0.5)
            part_units = t.units * frac
            px = c.close - d * spread_px if d < 0 else c.close
            t.partial_profit = (px - t.entry) * d * part_units \
                - self.cfg.commission_per_unit * part_units
            t.units -= part_units
            t.partial_done = True
        if mode in ("ATR_TRAIL", "STRUCT_TRAIL") and r_now >= 1.0:
            atr = tf_atr.get(t.tf, 0.0)
            if atr > 0:
                trail = c.close - d * mgmt.get("trail_atr", 2.0) * atr
                if (trail - t.stop) * d > 0:      # only ever tightens
                    t.stop = trail

    def _close(self, t: VirtualTrade, price: float, reason: str,
               now: datetime) -> None:
        d = 1 if t.direction == Direction.LONG else -1
        t.exit_price = price
        t.exit_time = now
        t.exit_reason = reason
        gross = (price - t.entry) * d * t.units
        costs = self.cfg.commission_per_unit * t.units
        t.profit = gross - costs + t.partial_profit
        t.r_multiple = t.profit / t.risk_money if t.risk_money > 0 else 0.0
        t.status = "closed"
        self.equity[t.strategy_id] = self.equity.get(
            t.strategy_id, self.cfg.virtual_equity) + t.profit
        del self.open[t.strategy_id]
        if reason == "STOP_LOSS" and self.cfg.shadow_post_watch_bars > 0:
            t.watch_bars_left = self.cfg.shadow_post_watch_bars
            self.watching.append(t)
        self.record_row(t)
        self.on_close(t)

    def _advance_watch(self, c: Candle) -> None:
        keep: List[VirtualTrade] = []
        for t in self.watching:
            if not t.watch_target_hit:
                d = 1 if t.direction == Direction.LONG else -1
                hit = (c.high >= t.target) if d > 0 else (c.low <= t.target)
                if hit:
                    t.watch_target_hit = True
            t.watch_bars_left -= 1
            if t.watch_bars_left > 0 and not t.watch_target_hit:
                keep.append(t)
            elif self.on_watch_done is not None:
                self.on_watch_done(t)      # resolved: tight-stop evidence
        self.watching = keep

    # ------------------------------------------------------------ state io
    def snapshot(self) -> dict:
        return {"equity": dict(self.equity), "serial": self._serial}

    def restore(self, snap: dict) -> None:
        self.equity = dict(snap.get("equity", {}))
        self._serial = int(snap.get("serial", 0))
        # open/pending virtual trades are intentionally NOT restored across
        # restarts: without tick continuity their management would be
        # unverifiable. They are logged as ABANDONED_RESTART by the caller.


#############################################################################
# V4 - STATISTICAL LEARNING
#############################################################################

"""
Statistical learning: performance tracking, risk-adjusted ranking,
exploration-vs-exploitation selection and controlled daily adaptation.

This is deliberately plain statistics — no "AI" claims:

RANKING (documented formula, applied identically to every strategy):
    shrunk_exp  = sum(R) / (n + k)          (k pulls small samples to 0)
    score       = shrunk_exp
                  - dd_penalty * max_drawdown_R
                  - instability_penalty * stdev(R)
                  - complexity_penalty * mutations
  A profitable strategy therefore ranks high only when its edge survives
  shrinkage, its drawdown is contained, its results are stable, and it did
  not need many parameter changes to look good (overfitting guard).

SELECTION for the single real DEMO position (UCB-style):
    eligible: active, >= min_shadow_trades, shrunk expectancy > 0
              (or regime-specific expectancy > 0 with enough regime trades)
    choose max( score + c * sqrt( ln(1+T) / (1+n_real) ) )
  so new-but-promising strategies still get real opportunities while
  proven ones are favoured — and one lucky win cannot dominate: with n=1
  and k=6, a +2.5R win shrinks to an expectancy of ~0.36R, below what a
  consistent performer accumulates over a real sample.

ADAPTATION (once per day, never after individual trades):
    * retire: n >= retire_min_trades and shrunk expectancy <= retire level
    * bench for a day: last-5 trades sum R <= bench threshold
    * spawn: bounded parameter variants of the best performers
    * parameter evidence nudges (e.g. >=40% of stops proved "too tight"
      over >= adapt_min_n trades -> widen that strategy's stop buffer one
      bounded step, as a new version)
All decisions are logged with their evidence and persisted.
"""


class StrategyStats:
    """Running performance record for one strategy (shadow + real)."""

    def __init__(self):
        self.n = 0
        self.wins = 0
        self.sum_r = 0.0
        self.sum_r2 = 0.0
        self.gross_win_r = 0.0
        self.gross_loss_r = 0.0
        self.cum_r = 0.0
        self.peak_r = 0.0
        self.max_dd_r = 0.0
        self.consec_losses = 0
        self.max_consec_losses = 0
        self.recent: List[float] = []          # last 10 R values
        self.mfe_sum = 0.0
        self.mae_sum = 0.0
        self.stop_tight = 0                    # stopped, target hit later
        self.stop_watch = 0                    # stop-outs watched
        self.target_far = 0                    # MFE >= 1.5R but lost
        self.early_entry = 0                   # won but MAE >= 0.6R
        self.by_regime: Dict[str, List[float]] = {}
        self.by_session: Dict[str, List[float]] = {}
        self.by_dow: Dict[str, List[float]] = {}
        self.n_real = 0
        self.sum_r_real = 0.0
        self.time_in_market_bars = 0

    # ------------------------------------------------------------ record
    def record(self, r: float, regime: str, session: str, dow: str,
               mfe: float, mae: float, bars: int, is_real: bool,
               stop_watched: bool = False, stop_tight: bool = False) -> None:
        self.n += 1
        self.sum_r += r
        self.sum_r2 += r * r
        if r > 0:
            self.wins += 1
            self.gross_win_r += r
            self.consec_losses = 0
            if mae >= 0.6:
                self.early_entry += 1
        else:
            self.gross_loss_r += -r
            self.consec_losses += 1
            self.max_consec_losses = max(self.max_consec_losses,
                                         self.consec_losses)
            if mfe >= 1.5:
                self.target_far += 1
        self.cum_r += r
        self.peak_r = max(self.peak_r, self.cum_r)
        self.max_dd_r = max(self.max_dd_r, self.peak_r - self.cum_r)
        self.recent = (self.recent + [r])[-10:]
        self.mfe_sum += mfe
        self.mae_sum += mae
        self.time_in_market_bars += bars
        if stop_watched:
            self.stop_watch += 1
            if stop_tight:
                self.stop_tight += 1
        self.by_regime.setdefault(regime, []).append(r)
        self.by_session.setdefault(session, []).append(r)
        self.by_dow.setdefault(dow, []).append(r)
        if is_real:
            self.n_real += 1
            self.sum_r_real += r

    # ------------------------------------------------------------ metrics
    def expectancy(self) -> float:
        return self.sum_r / self.n if self.n else 0.0

    def shrunk_expectancy(self, k: float) -> float:
        return self.sum_r / (self.n + k) if (self.n + k) > 0 else 0.0

    def std_r(self) -> float:
        if self.n < 2:
            return 1.0
        mean = self.sum_r / self.n
        var = max(0.0, self.sum_r2 / self.n - mean * mean)
        return math.sqrt(var)

    def profit_factor(self) -> float:
        if self.gross_loss_r <= 0:
            return float("inf") if self.gross_win_r > 0 else 0.0
        return self.gross_win_r / self.gross_loss_r

    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    def sortino(self) -> Optional[float]:
        """Only when there is enough data (n >= 15) and downside exists."""
        if self.n < 15:
            return None
        mean = self.expectancy()
        neg_var = 0.0
        neg_n = 0
        # approximate downside deviation from gross loss statistics
        if self.n - self.wins > 0:
            avg_loss = self.gross_loss_r / (self.n - self.wins)
            neg_var = avg_loss * avg_loss
            neg_n = self.n - self.wins
        if neg_n == 0 or neg_var == 0:
            return None
        return mean / math.sqrt(neg_var)

    def regime_expectancy(self, regime: str) -> Tuple[int, float]:
        rs = self.by_regime.get(regime, [])
        return len(rs), (sum(rs) / len(rs) if rs else 0.0)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @staticmethod
    def from_dict(d: dict) -> "StrategyStats":
        s = StrategyStats()
        for k, v in d.items():
            setattr(s, k, v)
        return s


class LearningBook:

    def __init__(self, cfg, log: Callable[[str], None]):
        self.cfg = cfg
        self.log = log
        self.stats: Dict[str, StrategyStats] = {}
        self.real_selections = 0
        self.decisions: List[dict] = []       # in-memory tail of learning log

    def get(self, sid: str) -> StrategyStats:
        return self.stats.setdefault(sid, StrategyStats())

    # ------------------------------------------------------------ intake
    def record_shadow(self, t: VirtualTrade) -> None:
        self.get(t.strategy_id).record(
            r=t.r_multiple, regime=t.regime, session=t.session,
            dow=(t.entry_time or t.signal_time).strftime("%a"),
            mfe=t.mfe_r, mae=t.mae_r, bars=t.bars_open, is_real=False)

    def record_shadow_postmortem(self, t: VirtualTrade) -> None:
        """Called when the post-stop watch window resolves."""
        s = self.get(t.strategy_id)
        s.stop_watch += 1
        if t.watch_target_hit:
            s.stop_tight += 1

    def record_real(self, sid: str, r: float, regime: str, session: str,
                    dow: str, mfe: float, mae: float, bars: int) -> None:
        self.get(sid).record(r=r, regime=regime, session=session, dow=dow,
                             mfe=mfe, mae=mae, bars=bars, is_real=True)

    # ------------------------------------------------------------ ranking
    def score(self, scfg: StrategyConfig) -> float:
        c = self.cfg
        s = self.get(scfg.sid)
        return (s.shrunk_expectancy(c.shrinkage_k)
                - c.dd_penalty * s.max_dd_r
                - c.instability_penalty * s.std_r()
                - c.complexity_penalty * scfg.mutations)

    def ranking(self, population: List[StrategyConfig]
                ) -> List[Tuple[StrategyConfig, float]]:
        rows = [(p, self.score(p)) for p in population
                if p.status != "retired"]
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows

    # ------------------------------------------------------------ selection
    def eligible_for_real(self, scfg: StrategyConfig, regime: str) -> bool:
        c = self.cfg
        if scfg.status != "active":
            return False
        s = self.get(scfg.sid)
        if s.n < c.min_shadow_trades_for_real:
            return False
        rn, rexp = s.regime_expectancy(regime)
        if rn >= c.min_regime_trades:
            return rexp > 0.0
        return s.shrunk_expectancy(c.shrinkage_k) > 0.0

    def select_real(self, candidates: List[Tuple[StrategyConfig, object]],
                    regime: str) -> Optional[Tuple[StrategyConfig, object]]:
        """candidates: (config, signal) pairs with fresh signals this bar."""
        c = self.cfg
        best = None
        best_v = -1e9
        total = 1 + self.real_selections
        for scfg, sig in candidates:
            if not self.eligible_for_real(scfg, regime):
                continue
            s = self.get(scfg.sid)
            ucb = c.exploration_c * math.sqrt(math.log(1 + total)
                                              / (1 + s.n_real))
            v = self.score(scfg) + ucb
            if v > best_v:
                best_v, best = v, (scfg, sig)
        if best is not None:
            self.real_selections += 1
        return best

    # ------------------------------------------------------------ adaptation
    def daily_update(self, population: List[StrategyConfig],
                     rng: random.Random, today_iso: str
                     ) -> Tuple[List[StrategyConfig], List[dict]]:
        """Controlled once-per-day evolution.  Returns (new population,
        decision log entries)."""
        c = self.cfg
        decisions: List[dict] = []

        def note(kind, sid, why):
            d = {"date": today_iso, "kind": kind, "sid": sid, "why": why}
            decisions.append(d)
            self.log(f"LEARNING [{kind}] {sid}: {why}")

        # un-bench strategies whose bench day has passed
        for p in population:
            if p.status == "benched" and p.bench_until < today_iso:
                p.status = "active"
                note("UNBENCH", p.sid, "bench period ended")

        # retire / bench on evidence
        for p in population:
            if p.status != "active":
                continue
            s = self.get(p.sid)
            shrunk = s.shrunk_expectancy(c.shrinkage_k)
            if s.n >= c.retire_min_trades and shrunk <= c.retire_expectancy:
                p.status = "retired"
                note("RETIRE", p.sid,
                     f"n={s.n} shrunk expectancy {shrunk:+.2f}R <= "
                     f"{c.retire_expectancy}")
                continue
            if len(s.recent) >= c.bench_recent_n and \
                    sum(s.recent[-c.bench_recent_n:]) <= c.bench_recent_sum_r:
                p.status = "benched"
                p.bench_until = today_iso
                note("BENCH", p.sid,
                     f"last {c.bench_recent_n} trades sum "
                     f"{sum(s.recent[-c.bench_recent_n:]):+.1f}R — benched "
                     f"for one day")

        # evidence-based parameter nudges (new bounded version)
        for p in list(population):
            if p.status != "active":
                continue
            s = self.get(p.sid)
            bounds = PARAM_BOUNDS[p.archetype]
            if s.stop_watch >= c.adapt_min_n and "buffer_atr" in bounds:
                rate = s.stop_tight / s.stop_watch
                if rate >= c.adapt_rate_threshold:
                    lo, hi = bounds["buffer_atr"]
                    old = p.params.get("buffer_atr", lo)
                    new = min(hi, old + 0.10)
                    if new > old:
                        p.params["buffer_atr"] = new
                        p.version += 1
                        s.stop_watch = 0
                        s.stop_tight = 0
                        note("ADAPT", p.sid,
                             f"{rate:.0%} of stop-outs later reached target "
                             f"-> stop buffer {old:.2f} -> {new:.2f} ATR "
                             f"(v{p.version})")
            if s.n >= c.adapt_min_n and s.n - s.wins > 0 and "rr" in bounds:
                far_rate = s.target_far / max(1, s.n - s.wins)
                if far_rate >= c.adapt_rate_threshold:
                    lo, hi = bounds["rr"]
                    old = p.params.get("rr", lo)
                    new = max(lo, old - 0.25)
                    if new < old:
                        p.params["rr"] = new
                        p.version += 1
                        s.target_far = 0
                        note("ADAPT", p.sid,
                             f"{far_rate:.0%} of losers reached >=1.5R "
                             f"before losing -> RR target {old:.2f} -> "
                             f"{new:.2f} (v{p.version})")

        # spawn bounded variants of the best performers
        active = [p for p in population if p.status == "active"]
        if len(population) < c.max_population:
            ranked = self.ranking(active)
            spawned = 0
            serial = sum(p.mutations for p in population) + 1
            for parent, sc in ranked:
                if spawned >= c.spawn_per_day:
                    break
                s = self.get(parent.sid)
                if s.n >= c.spawn_parent_min_n and sc > 0:
                    child = mutate_strategy(parent, rng, serial + spawned,
                                            today_iso)
                    population.append(child)
                    spawned += 1
                    note("SPAWN", child.sid,
                         f"variant of {parent.sid} (score {sc:+.3f}, "
                         f"n={s.n}) v{child.version}")
        self.decisions.extend(decisions)
        return population, decisions

    # ------------------------------------------------------------ state io
    def snapshot(self) -> dict:
        return {"stats": {sid: s.to_dict() for sid, s in self.stats.items()},
                "real_selections": self.real_selections}

    def restore(self, snap: dict) -> None:
        self.stats = {sid: StrategyStats.from_dict(d)
                      for sid, d in snap.get("stats", {}).items()}
        self.real_selections = int(snap.get("real_selections", 0))


#############################################################################
# V4 - RISK ENGINE + GUARD
#############################################################################

"""
V4 risk engine and guards.

* V4Guard extends the proven DailyLossGuard with the V4 rails:
    - daily lock at 1.7% COMBINED (realised + floating) loss,
    - weekly 5% drawdown lock (both from the base class, configured here),
    - a COOLDOWN after 3 consecutive real losses (instead of locking the
      whole day): no new entries for loss_cooldown_hours, then the streak
      counter resets. The cooldown timestamp is persisted, so restarting
      cannot bypass it.
* AdaptiveRisk chooses the per-trade risk fraction from evidence tiers and
  applies only *reductions* (drawdown, streaks, spread, volatility,
  remaining daily/weekly headroom). Nothing can raise risk above 0.75%,
  and risk NEVER increases because the previous trade lost.
* order_preflight is the full pre-order safety checklist for V4.
"""


class V4Guard(DailyLossGuard):

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cooldown_until: Optional[datetime] = None

    def register_close(self, profit: float,
                       now: Optional[datetime] = None) -> None:
        super().register_close(profit)
        if now is not None and profit < 0 and \
                self.consecutive_losses >= self.cfg.cooldown_after_losses:
            self.cooldown_until = now + timedelta(
                hours=self.cfg.loss_cooldown_hours)

    def lock_reason(self, equity: float, unrealised: float = 0.0,
                    session: Optional[SessionName] = None,
                    now: Optional[datetime] = None) -> LockReason:
        base = super().lock_reason(equity, unrealised, session)
        if base != LockReason.NONE:
            return base
        if now is not None and self.cooldown_until is not None:
            if now < self.cooldown_until:
                return LockReason.CONSECUTIVE_LOSSES
            # cooldown served: reset the streak, allow trading again
            self.cooldown_until = None
            self.consecutive_losses = 0
        return LockReason.NONE

    # -- persistence -----------------------------------------------------
    def snapshot(self) -> dict:
        d = {
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until.isoformat()
            if self.cooldown_until else "",
        }
        if self.day:
            d["day"] = {k: v for k, v in self.day.__dict__.items()}
        if self.week:
            d["week"] = {k: v for k, v in self.week.__dict__.items()}
        return d

    def restore(self, snap: dict, now: datetime) -> None:
        """Restore ONLY when the persisted period matches the current one,
        and never in a less restrictive direction than what the broker
        history already produced."""
        day = snap.get("day")
        if day and self.day and day.get("day") == self.day.day:
            merged = DayState(**{k: day[k] for k in day
                                 if k in DayState.__dataclass_fields__})
            # keep the WORSE (more restrictive) of persisted vs replayed
            self.day.realised = min(self.day.realised, merged.realised)
            self.day.trades_opened = max(self.day.trades_opened,
                                         merged.trades_opened)
            self.day.losses = max(self.day.losses, merged.losses)
            self.day.wins = max(self.day.wins, merged.wins)
            for k, v in merged.session_trades.items():
                self.day.session_trades[k] = max(
                    self.day.session_trades.get(k, 0), v)
        week = snap.get("week")
        if week and self.week and week.get("week") == self.week.week:
            self.week.realised = min(self.week.realised,
                                     float(week.get("realised", 0.0)))
            self.week.start_equity = float(week.get("start_equity",
                                                    self.week.start_equity))
            self.week.min_equity = min(self.week.min_equity,
                                       float(week.get("min_equity",
                                                      self.week.min_equity)))
        self.consecutive_losses = max(self.consecutive_losses,
                                      int(snap.get("consecutive_losses", 0)))
        cd = snap.get("cooldown_until", "")
        if cd:
            try:
                t = datetime.fromisoformat(cd)
                if t > now:
                    self.cooldown_until = t
            except ValueError:
                self.cooldown_until = None


class AdaptiveRisk:
    """Chooses the per-trade risk fraction. Reduction-only adjustments."""

    def __init__(self, cfg):
        self.cfg = cfg

    def tier(self, score: float, n: int) -> Tuple[float, float]:
        c = self.cfg
        if n >= c.tier_top_min_n and score >= c.tier_top_min_score:
            return c.risk_tier_top
        if n >= c.tier_strong_min_n and score >= c.tier_strong_min_score:
            return c.risk_tier_strong
        if n >= c.tier_moderate_min_n and score >= c.tier_moderate_min_score:
            return c.risk_tier_moderate
        return c.risk_tier_experimental

    def choose(self, score: float, n: int, *,
               equity: float, day_start_equity: float,
               daily_pl_combined: float, weekly_dd_frac: float,
               consecutive_losses: int, spread_points: float,
               atr_percentile: float, recent_strategy_r: float
               ) -> Tuple[float, List[str]]:
        """Returns (risk_fraction, notes). 0.0 means 'do not trade'."""
        c = self.cfg
        lo, hi = self.tier(score, n)
        risk = hi
        notes = [f"tier [{lo:.2%}..{hi:.2%}] from score {score:+.3f} n={n}"]

        def cut(mult: float, why: str):
            nonlocal risk
            new = risk * mult
            notes.append(f"reduce x{mult:.2f}: {why}")
            risk = new

        if consecutive_losses >= 1:
            cut(0.75 ** consecutive_losses,
                f"{consecutive_losses} consecutive losses")
        if weekly_dd_frac >= 0.025:
            cut(0.6, f"weekly drawdown {weekly_dd_frac:.1%} elevated")
        if spread_points > c.normal_spread_points:
            cut(0.7, f"spread {spread_points:.0f} pts above normal")
        if atr_percentile >= 0.90 or atr_percentile <= 0.05:
            cut(0.8, f"abnormal volatility (ATR pct {atr_percentile:.2f})")
        if recent_strategy_r < 0:
            cut(0.8, f"strategy recent form {recent_strategy_r:+.1f}R")
        risk = max(min(risk, hi), 0.0)

        # remaining DAILY headroom: never allow a loss to breach 1.7%
        if day_start_equity > 0:
            headroom = c.max_daily_loss + (daily_pl_combined
                                           / day_start_equity)
            headroom *= 0.9                       # safety margin
            if headroom <= 0:
                return 0.0, notes + ["no daily loss headroom left"]
            if risk > headroom:
                notes.append(f"capped by daily headroom {headroom:.2%}")
                risk = headroom
        # remaining WEEKLY headroom
        weekly_room = (c.max_weekly_drawdown - weekly_dd_frac) * 0.9
        if weekly_room <= 0:
            return 0.0, notes + ["no weekly drawdown headroom left"]
        if risk > weekly_room:
            notes.append(f"capped by weekly headroom {weekly_room:.2%}")
            risk = weekly_room

        risk = min(risk, c.max_risk_per_trade)
        if risk < c.min_risk_per_trade:
            return 0.0, notes + [
                f"risk {risk:.3%} below minimum useful {c.min_risk_per_trade:.2%}"]
        return risk, notes


@dataclass
class V4OrderFacts:
    is_demo_account: bool
    symbol_is_gold: bool
    market_open: bool
    spread_points: float
    spread_ok: bool
    positions_on_symbol: int
    has_pending_bot_order: bool
    news_blocked: bool
    news_reason: str
    lock: LockReason
    equity: float
    session_allowed: bool
    session_reason: str
    research_over: bool


@dataclass
class V4Preflight:
    ok: bool
    checks: List[str] = field(default_factory=list)
    reason: str = ""


def order_preflight(cfg, now: datetime, sig: Signal, entry: float,
                    volume_units: float, risk_money: float,
                    sizing_rejected: bool, sizing_reason: str,
                    facts: V4OrderFacts,
                    cooldown_active: bool) -> V4Preflight:
    checks: List[str] = []
    failures: List[str] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(f"{'PASS' if passed else 'FAIL'} — {name}: {detail}")
        if not passed:
            failures.append(f"{name}: {detail}")

    check("demo account", facts.is_demo_account,
          "demo" if facts.is_demo_account else "LIVE ACCOUNT — refused")
    check("gold symbol", facts.symbol_is_gold, "verified gold")
    check("research window", not facts.research_over,
          "active" if not facts.research_over
          else "14-day research complete — no new entries")
    check("market open", facts.market_open,
          "open" if facts.market_open else "closed")
    check("spread", facts.spread_ok,
          f"{facts.spread_points:.0f} pts (max {cfg.max_spread_points:.0f})")
    d = sig.direction.sign
    sl_ok = (sig.stop - entry) * d < 0
    tp_ok = (sig.target - entry) * d > 0
    check("stop loss present & on correct side", sl_ok,
          f"SL {sig.stop:.2f} vs entry {entry:.2f}")
    check("take profit present & on correct side", tp_ok,
          f"TP {sig.target:.2f}")
    net_rr = sig.rr(entry)
    check("reward:risk", net_rr >= cfg.min_net_rr * 0.9,
          f"{net_rr:.2f}R at fill reference (floor {cfg.min_net_rr:.1f})")
    check("volume valid", not sizing_rejected and volume_units > 0,
          f"{volume_units} units" if not sizing_rejected else sizing_reason)
    check("risk below maximum", facts.equity > 0 and not sizing_rejected
          and risk_money <= facts.equity * cfg.max_risk_per_trade * 1.0001,
          f"{risk_money:.2f} ({(risk_money / facts.equity if facts.equity else 0):.2%},"
          f" max {cfg.max_risk_per_trade:.2%})")
    check("no risk lock", facts.lock == LockReason.NONE,
          "clear" if facts.lock == LockReason.NONE
          else f"lock: {facts.lock.value}")
    check("no loss cooldown", not cooldown_active,
          "clear" if not cooldown_active else "post-loss cooldown active")
    check("one position rule", facts.positions_on_symbol == 0,
          f"{facts.positions_on_symbol} open on symbol")
    check("no duplicate pending order", not facts.has_pending_bot_order,
          "none" if not facts.has_pending_bot_order else "pending exists")
    check("news clear", not facts.news_blocked,
          "clear" if not facts.news_blocked else facts.news_reason)
    check("session allowed", facts.session_allowed, facts.session_reason)

    return V4Preflight(ok=not failures, checks=checks,
                       reason=failures[0] if failures else "")


#############################################################################
# V4 - PERSISTENCE
#############################################################################

"""
Restart-proof persistence for the 14-day research.

One JSON state file (atomic write: tmp + rename) carries everything that
must survive cTrader/computer restarts: the research start time, strategy
configurations and versions, performance statistics, rankings inputs,
guard state (so daily/weekly limits cannot be bypassed by restarting),
shadow book equity, counters and flags.

CSV files (append-only) record the research trail: shadow trades, real
trades, rejected setups, daily summaries, equity history, learning
decisions and parameter updates.  The final report is written as .txt and
.json.  No secrets are ever written.
"""


STATE_FILE = "research_state.json"

CSV_FIELDS: Dict[str, List[str]] = {
    "shadow_trades": [
        "trade_id", "strategy_id", "tf", "direction", "signal_time",
        "entry_time", "exit_time", "entry", "stop", "target", "exit_price",
        "exit_reason", "units", "risk_pct", "risk_money", "profit",
        "r_multiple", "mfe_r", "mae_r", "bars_open", "spread_points",
        "regime", "session", "mgmt_mode", "reason"],
    "real_trades": [
        "trade_id", "position_id", "strategy_id", "direction", "entry_time",
        "exit_time", "entry", "stop", "target", "exit_price", "exit_reason",
        "units", "risk_pct", "risk_money", "profit", "r_multiple", "mfe_r",
        "mae_r", "bars_open", "spread_points", "regime", "session",
        "reason"],
    "rejections": [
        "time", "stage", "strategy_id", "regime", "session",
        "spread_points", "reason"],
    "daily_summary": [
        "date", "day_index", "start_equity", "end_equity", "realised_pl",
        "realised_pct", "trades_real", "trades_shadow", "wins_real",
        "losses_real", "max_daily_dd_pct", "weekly_dd_pct",
        "active_strategies", "benched", "retired", "best_strategy",
        "best_score", "regime_mix", "lock_events"],
    "equity_history": [
        "time", "equity", "balance", "floating", "daily_pl_pct",
        "weekly_dd_pct"],
    "learning_log": ["date", "kind", "sid", "why"],
    "parameter_updates": ["date", "sid", "version", "param", "old", "new",
                          "evidence"],
}


class StateStore:

    def __init__(self, cfg, log: Callable[[str], None]):
        self.cfg = cfg
        self.log = log
        self.directory: Optional[str] = self._prepare_dir()
        self.enabled = self.directory is not None

    def _prepare_dir(self) -> Optional[str]:
        base = self.cfg.state_dir.strip() if self.cfg.state_dir else ""
        home = os.path.expanduser("~")
        candidates = []
        if base:
            candidates.append(base)
        candidates.append(os.path.join(home, "Documents",
                                       "XAUUSD_Adaptive_Bot_V4"))
        candidates.append(os.path.join(home, "XAUUSD_Adaptive_Bot_V4"))
        candidates.append(os.path.join(os.getcwd(),
                                       "XAUUSD_Adaptive_Bot_V4_state"))
        for cand in candidates:
            try:
                os.makedirs(cand, exist_ok=True)
                probe = os.path.join(cand, ".write_probe")
                with open(probe, "w", encoding="utf-8") as fh:
                    fh.write("ok")
                os.remove(probe)
                return os.path.abspath(cand)
            except OSError:
                continue
        self.log("PERSISTENCE: no writable directory — research state "
                 "CANNOT survive restarts. Fix folder permissions before "
                 "starting the 14-day run.")
        return None

    # ------------------------------------------------------------ json state
    def load_state(self) -> Optional[dict]:
        if not self.enabled:
            return None
        path = os.path.join(self.directory, STATE_FILE)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self.log(f"PERSISTENCE: state file unreadable ({exc}) — "
                     f"starting fresh but NOT deleting the old file")
            try:
                os.replace(path, path + ".corrupt")
            except OSError:
                pass
            return None

    def save_state(self, state: dict) -> None:
        if not self.enabled:
            return
        path = os.path.join(self.directory, STATE_FILE)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=1, default=str)
            os.replace(tmp, path)              # atomic on POSIX/macOS
        except OSError as exc:
            self.log(f"PERSISTENCE: state save failed ({exc})")

    # ------------------------------------------------------------ csv trail
    def csv_append(self, name: str, row: dict) -> None:
        if not self.enabled:
            return
        fields = CSV_FIELDS[name]
        path = os.path.join(self.directory, f"{name}.csv")
        try:
            fresh = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields,
                                        extrasaction="ignore", restval="")
                if fresh:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:
            self.log(f"PERSISTENCE: csv append to {name} failed ({exc})")

    def write_text(self, filename: str, text: str) -> None:
        if not self.enabled:
            return
        try:
            with open(os.path.join(self.directory, filename), "w",
                      encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            self.log(f"PERSISTENCE: write {filename} failed ({exc})")

    def write_json(self, filename: str, obj: dict) -> None:
        if not self.enabled:
            return
        try:
            with open(os.path.join(self.directory, filename), "w",
                      encoding="utf-8") as fh:
                json.dump(obj, fh, indent=1, default=str)
        except OSError as exc:
            self.log(f"PERSISTENCE: write {filename} failed ({exc})")


#############################################################################
# V4 - REPORTING
#############################################################################

"""
Daily and final reports for the 14-day research period.

The final report is honest about sample size: it labels the winner as
"PROVISIONAL WINNER — NOT AUTOMATICALLY READY FOR LIVE TRADING." and
states the confidence that the evidence actually supports, plus the
follow-up testing that must happen before any live consideration.
"""


PROVISIONAL_LABEL = ("PROVISIONAL WINNER — NOT AUTOMATICALLY READY FOR "
                     "LIVE TRADING.")


def _confidence(n: int) -> str:
    if n >= 100:
        return "MODERATE (>=100 trades — still requires out-of-sample proof)"
    if n >= 30:
        return "LOW (30-99 trades)"
    if n >= 10:
        return "VERY LOW (10-29 trades)"
    return "INSUFFICIENT (<10 trades — treat as anecdote, not evidence)"


def _fmt_stats(sid: str, scfg: Optional[StrategyConfig],
               s: StrategyStats) -> List[str]:
    pf = s.profit_factor()
    pf_s = f"{pf:.2f}" if pf != float("inf") else "inf"
    lines = [
        f"  {sid}"
        + (f" (v{scfg.version}, {scfg.archetype} @ {scfg.tf}, "
           f"mgmt {scfg.mgmt_mode}, status {scfg.status})" if scfg else ""),
        f"    trades {s.n} (real {s.n_real}) | win rate {s.win_rate():.0%} "
        f"| expectancy {s.expectancy():+.2f}R | PF {pf_s} "
        f"| max DD {s.max_dd_r:.1f}R | worst streak {s.max_consec_losses}",
        f"    total {s.sum_r:+.1f}R | avg MFE {s.mfe_sum / max(1, s.n):.2f}R "
        f"| avg MAE {s.mae_sum / max(1, s.n):.2f}R "
        f"| stop-too-tight {s.stop_tight}/{max(1, s.stop_watch)} "
        f"| target-too-far {s.target_far}",
    ]
    so = s.sortino()
    if so is not None:
        lines.append(f"    sortino(approx) {so:.2f}")
    if s.by_regime:
        parts = [f"{k}: n={len(v)} {sum(v) / len(v):+.2f}R"
                 for k, v in sorted(s.by_regime.items()) if v]
        lines.append("    by regime: " + "; ".join(parts))
    if s.by_session:
        parts = [f"{k}: n={len(v)} {sum(v) / len(v):+.2f}R"
                 for k, v in sorted(s.by_session.items()) if v]
        lines.append("    by session: " + "; ".join(parts))
    return lines


def best_by_regime(book: LearningBook, population: List[StrategyConfig]
                   ) -> Dict[str, Tuple[str, int, float]]:
    """regime -> (sid, n, expectancy) with minimum-evidence filter."""
    out: Dict[str, Tuple[str, int, float]] = {}
    for p in population:
        s = book.get(p.sid)
        for regime, rs in s.by_regime.items():
            if len(rs) < book.cfg.min_regime_trades:
                continue
            exp = sum(rs) / len(rs)
            cur = out.get(regime)
            if cur is None or exp > cur[2]:
                out[regime] = (p.sid, len(rs), exp)
    return out


def daily_report(now: datetime, day_index: int, research_days: int,
                 book: LearningBook, population: List[StrategyConfig],
                 account_lines: List[str]) -> str:
    ranked = book.ranking(population)
    lines = [
        "=" * 70,
        f"DAILY RESEARCH REPORT — day {day_index}/{research_days} — "
        f"{now:%Y-%m-%d %H:%M} UTC",
        "=" * 70, ""]
    lines += account_lines + [""]
    lines.append(f"population: {len(population)} strategies "
                 f"({sum(1 for p in population if p.status == 'active')} "
                 f"active, "
                 f"{sum(1 for p in population if p.status == 'benched')} "
                 f"benched, "
                 f"{sum(1 for p in population if p.status == 'retired')} "
                 f"retired)")
    lines.append("")
    lines.append("top 8 by risk-adjusted score:")
    for p, sc in ranked[:8]:
        s = book.get(p.sid)
        lines.append(f"  {sc:+.3f}  {p.sid} v{p.version}  n={s.n} "
                     f"exp={s.expectancy():+.2f}R dd={s.max_dd_r:.1f}R")
    lines.append("")
    lines.append("bottom 5:")
    for p, sc in ranked[-5:]:
        s = book.get(p.sid)
        lines.append(f"  {sc:+.3f}  {p.sid} v{p.version}  n={s.n} "
                     f"exp={s.expectancy():+.2f}R")
    return "\n".join(lines)


def final_report(now: datetime, research_start: datetime,
                 book: LearningBook, population: List[StrategyConfig],
                 account_summary: Dict[str, float],
                 equity_notes: List[str]) -> Tuple[str, dict]:
    """Returns (text, json_dict)."""
    cfg = book.cfg
    ranked = book.ranking(population)
    total_trades = sum(book.get(p.sid).n for p in population)
    total_real = sum(book.get(p.sid).n_real for p in population)
    by_regime = best_by_regime(book, population)
    winner, winner_score = (ranked[0] if ranked else (None, 0.0))

    lines = [
        "=" * 70,
        "FINAL 14-DAY RESEARCH REPORT — XAUUSD_Adaptive_Bot_V4",
        f"period: {research_start:%Y-%m-%d %H:%M} -> {now:%Y-%m-%d %H:%M} UTC",
        "=" * 70, "",
        PROVISIONAL_LABEL, "",
        f"total recorded trades: {total_trades} shadow+real "
        f"({total_real} real DEMO trades)",
        f"overall sample confidence: {_confidence(total_trades)}", ""]

    lines.append("---- account (real DEMO position results) ----")
    for k, v in account_summary.items():
        lines.append(f"  {k}: {v}")
    lines += [""] + equity_notes + [""]

    if winner is not None:
        s = book.get(winner.sid)
        lines.append("---- PROVISIONAL BEST OVERALL STRATEGY ----")
        lines += _fmt_stats(winner.sid, winner, s)
        lines.append(f"    ranking score: {winner_score:+.3f}")
        lines.append(f"    strategy sample confidence: {_confidence(s.n)}")
        lines.append("    why it ranked highly: positive shrunk expectancy "
                     "after drawdown/instability/complexity penalties — "
                     "i.e. its edge survived small-sample shrinkage and it "
                     "did not rely on one lucky streak.")
        weak = sorted(((k, sum(v) / len(v)) for k, v in
                       s.by_regime.items() if v), key=lambda x: x[1])
        if weak:
            lines.append(f"    weakest regime: {weak[0][0]} "
                         f"({weak[0][1]:+.2f}R avg) — avoid there.")
        lines.append(f"    recommended risk: experimental tier "
                     f"({cfg.risk_tier_experimental[0]:.2%}-"
                     f"{cfg.risk_tier_experimental[1]:.2%}) until the "
                     f"follow-up testing below is complete")
        lines.append(f"    recommended stop method: as configured "
                     f"(v{winner.version} params: "
                     + ", ".join(f"{k}={v:g}" for k, v in
                                 sorted(winner.params.items())) + ")")
        lines.append(f"    recommended exit management: {winner.mgmt_mode}")
        best_sess = sorted(((k, sum(v) / len(v)) for k, v in
                            s.by_session.items() if len(v) >= 3),
                           key=lambda x: -x[1])
        if best_sess:
            lines.append("    recommended sessions: "
                         + ", ".join(f"{k} ({v:+.2f}R)"
                                     for k, v in best_sess[:2]))
        lines.append("")

    lines.append("---- BEST STRATEGY BY MARKET REGIME "
                  f"(min {cfg.min_regime_trades} trades) ----")
    if by_regime:
        for regime, (sid, n, exp) in sorted(by_regime.items()):
            lines.append(f"  {regime:<18} {sid}  n={n}  {exp:+.2f}R avg")
    else:
        lines.append("  insufficient per-regime evidence")
    lines.append("")

    lines.append("---- FULL RANKING ----")
    for p, sc in ranked:
        s = book.get(p.sid)
        lines.append(f"  {sc:+.3f}  {p.sid:<28} v{p.version} "
                     f"[{p.status}] n={s.n} exp={s.expectancy():+.2f}R "
                     f"PF={'inf' if s.profit_factor() == float('inf') else f'{s.profit_factor():.2f}'} "
                     f"dd={s.max_dd_r:.1f}R")
    lines.append("")

    lines.append("---- WHAT THE LEARNING SYSTEM DID ----")
    kinds: Dict[str, int] = {}
    for d in book.decisions:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    lines.append("  decisions: " + (", ".join(f"{k}={v}" for k, v in
                                              sorted(kinds.items()))
                                    if kinds else "none recorded in memory"))
    lines.append("  losing-trade lessons: stop-too-tight and "
                 "target-too-far counters above show, per strategy, where "
                 "stops were inside noise and where targets were "
                 "unrealistic; ADAPT entries in learning_log.csv show every "
                 "evidence-based change and parameter_updates.csv whether "
                 "later performance improved or worsened.")
    lines.append("")

    lines.append("---- HONEST LIMITS OF THIS EXPERIMENT ----")
    lines.append(f"  {_confidence(total_trades)} overall; 14 days rarely "
                 "covers all market conditions for gold.")
    lines.append("  Required BEFORE any live consideration:")
    lines.append("    1. out-of-sample testing on data the strategies never "
                 "saw;")
    lines.append("    2. walk-forward testing across several windows;")
    lines.append("    3. at least 100 completed trades overall and a "
                 "meaningful sample per surviving strategy;")
    lines.append("    4. a longer forward-demo period with the selected "
                 "configuration frozen;")
    lines.append("    5. manual review of every ADAPT decision.")
    lines.append("")
    lines.append("  The DEMO-only lock remains in force. Nothing in this "
                 "report enables live trading.")

    js = {
        "label": PROVISIONAL_LABEL,
        "generated": now.isoformat(),
        "research_start": research_start.isoformat(),
        "total_trades": total_trades,
        "total_real_trades": total_real,
        "confidence": _confidence(total_trades),
        "account": account_summary,
        "winner": ({"sid": winner.sid, "version": winner.version,
                    "archetype": winner.archetype, "tf": winner.tf,
                    "params": winner.params, "mgmt": winner.mgmt,
                    "score": winner_score,
                    "stats": book.get(winner.sid).to_dict()}
                   if winner else None),
        "best_by_regime": {k: {"sid": v[0], "n": v[1], "expectancy": v[2]}
                           for k, v in by_regime.items()},
        "ranking": [{"sid": p.sid, "version": p.version, "status": p.status,
                     "score": sc, "n": book.get(p.sid).n,
                     "expectancy": book.get(p.sid).expectancy()}
                    for p, sc in ranked],
    }
    return "\n".join(lines), js


#############################################################################
# THE cBOT (the only section that talks to the cTrader API)
#############################################################################

BOT_LABEL = "XAUUSD_Adaptive_Bot_V4"

DECISION_TFS = (Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1)


class XAUUSD_Adaptive_Bot_V4(object):

    # ------------------------------------------------------------ lifecycle
    def on_start(self):
        self._fatal = False
        self._real = None                # dict record of the open real trade
        self._last_m1_open = None
        self._last_bucket = {tf: None for tf in DECISION_TFS}
        self._features = {}              # tf -> TFFeatures (latest)
        self._regime = Regime.UNSAFE.value
        self._last_heartbeat = None
        self._last_state_save = None
        self._fresh_signals = []         # (StrategyConfig, Signal) this bar
        self._lock_logged = None

        self.cfg = V4Config()
        validator = V4ConfigValidator(self.cfg)
        if not validator.validate():
            for line in validator.report().splitlines():
                api.Print(line)
            api.Print("configuration invalid — stopping")
            self._fatal = True
            api.Stop()
            return
        for line in validator.report().splitlines():
            if line:
                api.Print(line)

        # ---- DEMO-ONLY LOCK (no live switch exists anywhere) ---------------
        if bool(api.Account.IsLive):
            api.Print("LIVE ACCOUNT BLOCKED")
            api.Print("V4 is a demo-only research bot. Connect the Skilling "
                      "DEMO account and restart.")
            self._fatal = True
            api.Stop()
            return
        api.Print("account check: DEMO account confirmed")

        # ---- GOLD-ONLY LOCK --------------------------------------------------
        self._symbol_name = str(api.SymbolName)
        if not self._symbol_is_gold(self._symbol_name):
            api.Print(f"SYMBOL BLOCKED: '{self._symbol_name}' is not an "
                      f"approved gold symbol. EURUSD and all non-gold "
                      f"symbols are refused.")
            self._fatal = True
            api.Stop()
            return
        api.Print(f"symbol check: {self._symbol_name} accepted as GOLD")

        self.spec = self._build_spec()
        ok, why = self.spec.valid()
        if not ok:
            api.Print(f"SYMBOL SPEC UNVERIFIABLE: {why} — stopping")
            self._fatal = True
            api.Stop()
            return
        mpu = self.spec.money_per_price_unit_per_unit()
        api.Print(f"symbol spec: tick {self.spec.tick_size} | 1 unit per "
                  f"1.0 move = {mpu:.4f} {str(api.Account.Currency)} | "
                  f"volume min/step/max {self.spec.volume_min}/"
                  f"{self.spec.volume_step}/{self.spec.volume_max} units")
        if not (0.1 <= mpu <= 10.0):
            api.Print("per-unit value implausible for gold — refusing to "
                      "trade until verified")
            self._fatal = True
            api.Stop()
            return

        # ---- research stack ----------------------------------------------------
        self.store = StateStore(self.cfg, lambda m: api.Print(m))
        self.sessions = SessionManager(self.cfg)
        self.news = NewsFilter(self.cfg, UTC)
        self.spread_filter = SpreadFilter(self.cfg)
        self.features_builder = FeatureBuilder(self.cfg)
        self.regime_detector = MarketRegimeDetector(self.cfg)
        self.sizer = PositionSizer(self.cfg)
        self.guard = V4Guard(self.cfg)
        self.risk = AdaptiveRisk(self.cfg)
        self.book = LearningBook(self.cfg, lambda m: api.Print(m))
        self.shadow = ShadowEngine(
            self.cfg, lambda m: api.Print(m),
            on_close=self._on_shadow_close,
            record_row=self._shadow_row,
            on_watch_done=self.book.record_shadow_postmortem)
        self.rng = random.Random(self.cfg.population_seed)

        # ---- news honesty ------------------------------------------------------
        for bad in self.news.malformed_entries():
            api.Print(f"NEWS CONFIG: ignoring malformed entry: {bad}")
        if self.cfg.news_enabled and not (self.cfg.fomc_events
                                          or self.cfg.cpi_events):
            api.Print("*** NEWS DATES MISSING *** only the NFP first-Friday "
                      "rule is active. Add FOMC/CPI dates to config or set "
                      "require_news_calendar=True to block entries entirely.")
        api.Print("NEWS PROTECTION: schedule-based/manual only — no live "
                  "feed exists inside cTrader Python and none is claimed.")

        # ---- market data --------------------------------------------------------
        self._tf_map = {
            Timeframe.M1: TimeFrame.Minute,
            Timeframe.M5: TimeFrame.Minute5,
            Timeframe.M15: TimeFrame.Minute15,
            Timeframe.M30: TimeFrame.Minute30,
            Timeframe.H1: TimeFrame.Hour,
        }
        self._windows = {
            Timeframe.M1: self.cfg.window_m1,
            Timeframe.M5: self.cfg.window_m5,
            Timeframe.M15: self.cfg.window_m15,
            Timeframe.M30: self.cfg.window_m30,
            Timeframe.H1: self.cfg.window_h1,
        }
        self._bars = {tf: api.MarketData.GetBars(ctf)
                      for tf, ctf in self._tf_map.items()}

        now = self._now_utc()
        equity = float(api.Account.Equity)
        balance = float(api.Account.Balance)

        # ---- restart-proof research state ---------------------------------------
        state = self.store.load_state() or {}
        rs = state.get("research_start", "")
        if rs:
            try:
                self.research_start = datetime.fromisoformat(rs)
            except ValueError:
                self.research_start = now
        else:
            self.research_start = now
            api.Print(f"RESEARCH CLOCK STARTED: {now:%Y-%m-%d %H:%M} UTC "
                      f"(+{self.cfg.research_days} days)")
        self.final_report_done = bool(state.get("final_report_done", False))
        pop = state.get("population")
        if pop:
            self.population = [StrategyConfig.from_dict(d) for d in pop]
            api.Print(f"restored {len(self.population)} strategies from "
                      f"persisted state")
        else:
            self.population = seed_population(now.date().isoformat())
            api.Print(f"seeded {len(self.population)} initial strategies "
                      f"across 8 archetypes")
        if state.get("learning"):
            self.book.restore(state["learning"])
        if state.get("shadow"):
            self.shadow.restore(state["shadow"])
        self._mutation_serial = int(state.get("counters", {})
                                    .get("mutation_serial", 0))
        self._last_day_reported = state.get("last_day_reported", "")

        # guards: broker-history replay first, then persisted state merge —
        # limits can only get MORE restrictive, never less
        self.guard.roll(now, equity, balance)
        self._replay_today_history(now)
        if state.get("guard"):
            self.guard.restore(state["guard"], now)
        api.Print(f"daily guard: {self.guard.describe(equity)}")

        m5 = self._completed_candles(Timeframe.M5)
        self.sessions.rebuild_from(m5)

        self._adopt_open_position(state.get("open_real"), now)

        m1 = self._completed_candles(Timeframe.M1)
        if m1:
            self._last_m1_open = m1[-1].time
            for tf in DECISION_TFS:
                self._last_bucket[tf] = tf_bucket_start(m1[-1].time, tf)

        day_idx = self._day_index(now)
        api.Print(f"{BOT_LABEL} started | DEMO | day {day_idx}/"
                  f"{self.cfg.research_days} of research | "
                  f"{len(self.population)} strategies | max risk "
                  f"{self.cfg.max_risk_per_trade:.2%}/trade, daily "
                  f"{self.cfg.max_daily_loss:.1%} combined, weekly "
                  f"{self.cfg.max_weekly_drawdown:.0%} | state: "
                  f"{self.store.directory or 'NOT PERSISTED — FIX THIS'}")
        api.Print("no trading at startup — first decisions on the next "
                  "completed decision-timeframe candle")
        self._save_state(now)

    def on_tick(self):
        if self._fatal:
            return
        try:
            self._tick()
        except Exception as exc:
            api.Print(f"ERROR in tick processing: {exc!r}")

    def on_stop(self):
        if self._fatal:
            return
        now = self._now_utc()
        self._save_state(now)
        api.Print(f"{BOT_LABEL} stopping. "
                  f"{self.guard.describe(float(api.Account.Equity))}")
        if self._real is not None:
            api.Print(f"NOTE: real position {self._real['position_id']} "
                      f"stays protected by broker-side SL/TP.")
        if self.store.directory:
            api.Print(f"research files: {self.store.directory}")

    # ------------------------------------------------------------ main tick
    def _tick(self):
        m1_bars = self._bars[Timeframe.M1]
        if int(m1_bars.Count) < 3:
            return
        last_open = self._to_utc(m1_bars.OpenTimes[int(m1_bars.Count) - 2])
        if self._last_m1_open is not None and last_open == self._last_m1_open:
            return
        self._last_m1_open = last_open

        m1 = self._completed_candles(Timeframe.M1)
        if not m1:
            return
        candle = m1[-1]
        now = candle.time + timedelta(minutes=1)
        equity = float(api.Account.Equity)
        balance = float(api.Account.Balance)
        spread_points = self._spread_points()

        self.sessions.update_ranges(candle)
        new_day, _ = self.guard.roll(now, equity, balance)
        if new_day:
            self._daily_pipeline(now, equity)
        self.guard.emergency = self._emergency_present()

        self._reconcile_real(now)
        self._update_real_excursions(candle)
        self._manage_real(candle, now, spread_points)

        # shadow books advance on every completed M1 candle
        mgmt_lookup = {p.sid: p.mgmt for p in self.population}
        tf_atr = {tf.value: (self._features.get(tf).atr_now
                             if self._features.get(tf) else 0.0)
                  for tf in DECISION_TFS}
        self.shadow.on_m1(candle, spread_points, self.spec.point, now,
                          mgmt_lookup, tf_atr)

        # decision timeframes: features + strategy evaluation on bar close
        self._fresh_signals = []
        for tf in DECISION_TFS:
            bucket = tf_bucket_start(candle.time, tf)
            if bucket == self._last_bucket[tf]:
                continue
            self._last_bucket[tf] = bucket
            self._on_tf_close(tf, now, spread_points)

        # a single real DEMO position, chosen by evidence
        if self._fresh_signals and self._real is None:
            self._consider_real(now, equity, spread_points)

        self._heartbeat(now, spread_points, equity)
        if self._last_state_save is None or \
                (now - self._last_state_save) >= timedelta(minutes=15):
            self._save_state(now)

    # ------------------------------------------------------------ tf close
    def _on_tf_close(self, tf, now, spread_points):
        candles = self._completed_candles(tf)
        marks = self.sessions.marks_for(now.date()) \
            if tf in (Timeframe.M5, Timeframe.M15) else None
        f = self.features_builder.build(tf, candles, marks)
        self._features[tf] = f
        if f is None:
            return
        if tf == Timeframe.M15:
            news_blocked, _ = self.news.blackout(now)
            reading = self.regime_detector.classify(
                f.candles, f.structure, spread_points, news_blocked)
            if reading.regime.value != self._regime:
                api.Print(f"regime: {self._regime} -> "
                          f"{reading.regime.value} ({reading.reason})")
            self._regime = reading.regime.value

        if self._research_over(now):
            return                        # no new research entries
        session = self.sessions.session_at(now).value
        ctx = EvalContext(
            regime=self._regime, session=session,
            spread_points=spread_points, point=self.spec.point, now=now,
            cost=self._cost_price_units(spread_points),
            min_net_rr=self.cfg.min_net_rr,
            asian_range=self.sessions.asian_range(now.date()),
            minutes_into_london=self._minutes_into(now,
                                                   self.cfg.london_start),
            minutes_into_ny=self._minutes_into(now, self.cfg.ny_start))

        shadow_ok, shadow_why = self._shadow_gates(now, spread_points)
        for p in self.population:
            if p.tf != tf.value or p.status != "active":
                continue
            try:
                sig = evaluate_strategy(p, f, ctx)
            except Exception as exc:
                api.Print(f"strategy {p.sid} crashed in evaluation: {exc!r}")
                continue
            if sig is None:
                continue
            self._fresh_signals.append((p, sig))
            if shadow_ok:
                if self.shadow.submit(p, sig, spread_points,
                                      self.spec.point, self._regime,
                                      session):
                    self._debug(f"shadow open queued: {p.sid} "
                                f"{sig.direction.value} stop {sig.stop:.2f} "
                                f"tgt {sig.target:.2f} ({sig.reason})")
            else:
                self._reject_row(now, "shadow-gate", p.sid, spread_points,
                                 shadow_why)

    def _shadow_gates(self, now, spread_points):
        if now.weekday() >= 5:
            return False, "weekend"
        if not bool(api.Symbol.MarketHours.IsOpened()):
            return False, "market closed"
        blocked, why = self.news.blackout(now)
        if blocked:
            return False, f"news blackout: {why}"
        ok, why = self.spread_filter.check(spread_points)
        if not ok:
            return False, why
        return True, ""

    # ------------------------------------------------------------ real entry
    def _consider_real(self, now, equity, spread_points):
        cfg = self.cfg
        if self._research_over(now):
            return
        floating = self._floating_pnl()
        session_obj = self.sessions.session_at(now)
        lock = self.guard.lock_reason(equity, floating, session_obj, now)
        if lock != LockReason.NONE:
            if lock.value != self._lock_logged:
                api.Print(f"no real entries — lock: {lock.value} | "
                          f"{self.guard.describe(equity)}")
                self._lock_logged = lock.value
            return
        self._lock_logged = None
        allowed, window_why = self.sessions.entry_window_check(now)
        if not allowed:
            self._debug(f"real entry window closed: {window_why}")
            return
        news_blocked, news_reason = self.news.blackout(now)
        if cfg.require_news_calendar and not (cfg.fomc_events
                                              or cfg.cpi_events):
            news_blocked, news_reason = True, \
                "require_news_calendar=True but no dates configured"
        if news_blocked:
            self._reject_row(now, "news", "-", spread_points, news_reason)
            return
        spread_ok, spread_why = self.spread_filter.check(spread_points)
        if not spread_ok:
            self._reject_row(now, "spread", "-", spread_points, spread_why)
            return

        chosen = self.book.select_real(self._fresh_signals, self._regime)
        if chosen is None:
            self._debug("no strategy is eligible for a real trade yet "
                        "(insufficient shadow evidence or negative "
                        "expectancy)")
            return
        scfg, sig = chosen
        stats = self.book.get(scfg.sid)
        day = self.guard.day
        daily_pl = (day.realised if day else 0.0) + min(0.0, floating)
        weekly_dd = 0.0
        if self.guard.week and self.guard.week.start_equity > 0:
            weekly_dd = max(0.0, (self.guard.week.start_equity
                                  - min(self.guard.week.min_equity, equity))
                            / self.guard.week.start_equity)
        f15 = self._features.get(Timeframe.M15)
        risk_frac, notes = self.risk.choose(
            self.book.score(scfg), stats.n, equity=equity,
            day_start_equity=day.start_equity if day else equity,
            daily_pl_combined=daily_pl, weekly_dd_frac=weekly_dd,
            consecutive_losses=self.guard.consecutive_losses,
            spread_points=spread_points,
            atr_percentile=f15.atr_percentile if f15 else 0.5,
            recent_strategy_r=sum(stats.recent[-3:]))
        for n in notes:
            self._debug(f"risk: {n}")
        if risk_frac <= 0:
            self._reject_row(now, "risk", scfg.sid, spread_points,
                             "; ".join(notes[-2:]))
            return

        entry = float(api.Symbol.Ask) if sig.direction == Direction.LONG \
            else float(api.Symbol.Bid)
        self.spec.spread_points = spread_points
        sizing = self.sizer.size(self.spec, equity, risk_frac, entry,
                                 sig.stop)
        facts = V4OrderFacts(
            is_demo_account=not bool(api.Account.IsLive),
            symbol_is_gold=self._symbol_is_gold(self._symbol_name),
            market_open=bool(api.Symbol.MarketHours.IsOpened()),
            spread_points=spread_points, spread_ok=spread_ok,
            positions_on_symbol=self._symbol_positions_count(),
            has_pending_bot_order=self._has_pending_order(),
            news_blocked=news_blocked, news_reason=news_reason,
            lock=lock, equity=equity, session_allowed=allowed,
            session_reason=window_why,
            research_over=self._research_over(now))
        pf = order_preflight(cfg, now, sig, entry, sizing.volume_units,
                             sizing.risk_money, sizing.rejected,
                             sizing.reason, facts,
                             cooldown_active=self.guard.cooldown_until
                             is not None and now < self.guard.cooldown_until)
        if cfg.debug_logging or not pf.ok:
            for line in pf.checks:
                api.Print(f"  order-safety {line}")
        if not pf.ok:
            self._reject_row(now, "preflight", scfg.sid, spread_points,
                             pf.reason)
            return

        trade_type = TradeType.Buy if sig.direction == Direction.LONG \
            else TradeType.Sell
        sl_pips = abs(entry - sig.stop) / self.spec.pip_size
        tp_pips = abs(sig.target - entry) / self.spec.pip_size
        result = api.ExecuteMarketOrder(trade_type, self._symbol_name,
                                        sizing.volume_units, BOT_LABEL,
                                        sl_pips, tp_pips)
        if not bool(result.IsSuccessful) or result.Position is None:
            err = str(result.Error) if result.Error is not None else "unknown"
            api.Print(f"ORDER FAILED: {err}")
            self._reject_row(now, "broker", scfg.sid, spread_points,
                             f"order rejected: {err}")
            return
        pos = result.Position
        fill = float(pos.EntryPrice)
        sl_price = round(sig.stop, self.spec.digits)
        tp_price = round(sig.target, self.spec.digits)
        try:
            api.ModifyPosition(pos, sl_price, tp_price)
        except Exception as exc:
            api.Print(f"note: SL/TP refine failed ({exc!r}); pip-based "
                      f"protection from fill remains")
        self._real = {
            "trade_id": new_id("real"), "position_id": int(pos.Id),
            "strategy_id": scfg.sid, "direction": sig.direction.value,
            "entry": fill, "stop": sl_price, "initial_stop": sl_price,
            "target": tp_price, "units": sizing.volume_units,
            "risk_money": sizing.risk_money,
            "risk_pct": sizing.risk_fraction_actual,
            "entry_time": now.isoformat(), "regime": self._regime,
            "session": session_obj.value, "reason": sig.reason,
            "spread_points": spread_points, "mfe_r": 0.0, "mae_r": 0.0,
            "bars_open": 0, "be_done": False, "partial_done": False,
            "mgmt": dict(scfg.mgmt), "mgmt_mode": scfg.mgmt_mode,
            "tf": scfg.tf,
        }
        self.guard.register_open(session_obj)
        api.Print(f"REAL ORDER FILLED [{scfg.sid} v{scfg.version}]: "
                  f"{sig.direction.value} {sizing.volume_units} units @ "
                  f"{fill:.2f} SL {sl_price:.2f} TP {tp_price:.2f} risk "
                  f"{sizing.risk_money:.2f} ({sizing.risk_fraction_actual:.2%}) "
                  f"| {sig.reason}")
        self._save_state(now)

    # ------------------------------------------------------------ real upkeep
    def _reconcile_real(self, now):
        if self._real is None:
            return
        r = self._real
        if self._find_position(r["position_id"]) is not None:
            return
        h = self._find_history(r["position_id"])
        profit = float(h.NetProfit) if h is not None else 0.0
        exit_price = float(h.ClosingPrice) if h is not None else 0.0
        exit_time = self._to_utc(h.ClosingTime) if h is not None else now
        r_mult = profit / r["risk_money"] if r["risk_money"] > 0 else 0.0
        self.guard.register_close(profit, now)
        self.book.record_real(
            r["strategy_id"], r_mult, r["regime"], r["session"],
            exit_time.strftime("%a"), r["mfe_r"], r["mae_r"],
            r["bars_open"])
        self.store.csv_append("real_trades", {
            "trade_id": r["trade_id"], "position_id": r["position_id"],
            "strategy_id": r["strategy_id"], "direction": r["direction"],
            "entry_time": r["entry_time"],
            "exit_time": exit_time.isoformat(), "entry": f"{r['entry']:.2f}",
            "stop": f"{r['initial_stop']:.2f}",
            "target": f"{r['target']:.2f}",
            "exit_price": f"{exit_price:.2f}",
            "exit_reason": self._infer_exit(r, exit_price),
            "units": r["units"], "risk_pct": f"{r['risk_pct']:.4%}",
            "risk_money": f"{r['risk_money']:.2f}",
            "profit": f"{profit:.2f}", "r_multiple": f"{r_mult:.2f}",
            "mfe_r": f"{r['mfe_r']:.2f}", "mae_r": f"{r['mae_r']:.2f}",
            "bars_open": r["bars_open"],
            "spread_points": f"{r['spread_points']:.0f}",
            "regime": r["regime"], "session": r["session"],
            "reason": r["reason"]})
        api.Print(f"REAL TRADE CLOSED [{r['strategy_id']}]: "
                  f"{profit:+.2f} ({r_mult:+.2f}R) | "
                  f"{self.guard.describe(float(api.Account.Equity))}")
        self._real = None
        self._save_state(now)

    def _infer_exit(self, r, exit_price):
        if exit_price <= 0:
            return "BROKER_CLOSED"
        tol = 3 * self.spec.tick_size + self.spec.spread_points * self.spec.point
        if abs(exit_price - r["stop"]) <= tol:
            return "STOP_LOSS"
        if abs(exit_price - r["target"]) <= tol:
            return "TAKE_PROFIT"
        return "MANAGED_EXIT"

    def _update_real_excursions(self, candle):
        r = self._real
        if r is None:
            return
        risk = abs(r["entry"] - r["initial_stop"])
        if risk <= 0:
            return
        d = 1 if r["direction"] == "LONG" else -1
        fav = ((candle.high - r["entry"]) if d > 0
               else (r["entry"] - candle.low)) / risk
        adv = ((r["entry"] - candle.low) if d > 0
               else (candle.high - r["entry"])) / risk
        r["mfe_r"] = max(r["mfe_r"], fav)
        r["mae_r"] = max(r["mae_r"], adv)
        r["bars_open"] += 1

    def _manage_real(self, candle, now, spread_points):
        r = self._real
        if r is None:
            return
        pos = self._find_position(r["position_id"])
        if pos is None:
            return
        d = 1 if r["direction"] == "LONG" else -1
        risk = abs(r["entry"] - r["initial_stop"])
        if risk <= 0:
            return
        r_now = (candle.close - r["entry"]) * d / risk
        mode = r["mgmt_mode"]
        mgmt = r["mgmt"]
        # weekend flat
        if self.sessions.near_weekend_flat(now) \
                and not self.cfg.weekend_hold_allowed:
            try:
                api.ClosePosition(pos)
                api.Print("real position closed: pre-weekend flat")
            except Exception as exc:
                api.Print(f"weekend close failed: {exc!r}")
            return
        new_stop = None
        if mode in ("BE_1R", "PARTIAL_RUNNER", "ATR_TRAIL", "STRUCT_TRAIL") \
                and not r["be_done"] and r_now >= mgmt.get("be_r", 1.2):
            be = r["entry"] + d * (spread_points
                                   + self.cfg.slippage_buffer_points) \
                * self.spec.point
            if (be - r["stop"]) * d > 0:
                new_stop = be
            r["be_done"] = True
        if mode == "PARTIAL_RUNNER" and not r["partial_done"] \
                and r_now >= mgmt.get("partial_r", 1.8):
            vol = self.spec.round_volume_down(
                r["units"] * mgmt.get("partial_frac", 0.5))
            if vol >= self.spec.volume_min and vol < r["units"]:
                try:
                    res = api.ClosePosition(pos, vol)
                    if bool(res.IsSuccessful):
                        r["units"] -= vol
                        r["partial_done"] = True
                        api.Print(f"real partial close {vol} units at "
                                  f"+{r_now:.2f}R")
                except Exception as exc:
                    api.Print(f"partial close failed: {exc!r}")
        if mode in ("ATR_TRAIL", "STRUCT_TRAIL") and r_now >= 1.0:
            f = self._features.get(Timeframe(r["tf"])) \
                if r["tf"] in ("M5", "M15", "M30", "H1") else None
            atr = f.atr_now if f else 0.0
            if atr > 0:
                trail = candle.close - d * mgmt.get("trail_atr", 2.0) * atr
                if new_stop is None or (trail - new_stop) * d > 0:
                    if (trail - r["stop"]) * d > 0:
                        new_stop = trail
        if new_stop is not None and (new_stop - r["stop"]) * d > 0:
            price = round(new_stop, self.spec.digits)
            try:
                api.ModifyPosition(pos, price, pos.TakeProfit)
                r["stop"] = price
                api.Print(f"real stop tightened to {price:.2f} "
                          f"(stops never widen)")
            except Exception as exc:
                api.Print(f"stop move failed: {exc!r}")

    # ------------------------------------------------------------ research ops
    def _research_over(self, now) -> bool:
        return (now - self.research_start) >= timedelta(
            days=self.cfg.research_days)

    def _day_index(self, now) -> int:
        return min(self.cfg.research_days,
                   (now - self.research_start).days + 1)

    def _daily_pipeline(self, now, equity):
        today = now.date().isoformat()
        if self._last_day_reported == today:
            return
        self._last_day_reported = today
        day_idx = self._day_index(now)
        api.Print(f"=== new trading day: research day {day_idx}/"
                  f"{self.cfg.research_days} ===")
        acct = [f"equity {equity:.2f} | {self.guard.describe(equity)}"]
        rep = daily_report(now, day_idx, self.cfg.research_days, self.book,
                           self.population, acct)
        self.store.write_text(f"daily_report_{today}.txt", rep)
        ranked = self.book.ranking(self.population)
        best = ranked[0] if ranked else None
        self.store.csv_append("daily_summary", {
            "date": today, "day_index": day_idx,
            "start_equity": f"{equity:.2f}", "end_equity": "",
            "realised_pl": f"{self.guard.day.realised:.2f}"
            if self.guard.day else "0",
            "realised_pct": "", "trades_real": self.guard.day.trades_opened
            if self.guard.day else 0,
            "trades_shadow": sum(self.book.get(p.sid).n
                                 for p in self.population),
            "wins_real": self.guard.day.wins if self.guard.day else 0,
            "losses_real": self.guard.day.losses if self.guard.day else 0,
            "max_daily_dd_pct": "", "weekly_dd_pct": "",
            "active_strategies": sum(1 for p in self.population
                                     if p.status == "active"),
            "benched": sum(1 for p in self.population
                           if p.status == "benched"),
            "retired": sum(1 for p in self.population
                           if p.status == "retired"),
            "best_strategy": best[0].sid if best else "",
            "best_score": f"{best[1]:+.3f}" if best else "",
            "regime_mix": self._regime, "lock_events": ""})

        if not self._research_over(now):
            self.population, decisions = self.book.daily_update(
                self.population, self.rng, today)
            for d in decisions:
                self.store.csv_append("learning_log", d)
                if d["kind"] == "ADAPT":
                    self.store.csv_append("parameter_updates", {
                        "date": d["date"], "sid": d["sid"], "version": "",
                        "param": "", "old": "", "new": "",
                        "evidence": d["why"]})
        elif not self.final_report_done:
            self._write_final_report(now, equity)
        self._save_state(now)

    def _write_final_report(self, now, equity):
        api.Print("=== 14-DAY RESEARCH COMPLETE — writing final report ===")
        acct = {
            "final_equity": f"{equity:.2f}",
            "week_realised": f"{self.guard.week.realised:.2f}"
            if self.guard.week else "0",
            "real_trades_total": sum(self.book.get(p.sid).n_real
                                     for p in self.population),
        }
        notes = [f"research ran {self.research_start:%Y-%m-%d} -> "
                 f"{now:%Y-%m-%d}; full trails in equity_history.csv, "
                 f"daily_summary.csv, shadow_trades.csv, real_trades.csv"]
        text, js = final_report(now, self.research_start, self.book,
                                self.population, acct, notes)
        self.store.write_text("final_report.txt", text)
        self.store.write_json("final_report.json", js)
        for line in text.splitlines()[:12]:
            api.Print(line)
        api.Print(f"full report: "
                  f"{os.path.join(self.store.directory or '', 'final_report.txt')}")
        self.final_report_done = True

    # ------------------------------------------------------------ callbacks
    def _on_shadow_close(self, t):
        self.book.record_shadow(t)
        self._debug(f"shadow closed: {t.strategy_id} {t.exit_reason} "
                    f"{t.r_multiple:+.2f}R (MFE {t.mfe_r:.2f}/MAE "
                    f"{t.mae_r:.2f})")

    def _shadow_row(self, t):
        self.store.csv_append("shadow_trades", {
            "trade_id": t.trade_id, "strategy_id": t.strategy_id,
            "tf": t.tf, "direction": t.direction.value,
            "signal_time": t.signal_time.isoformat(),
            "entry_time": t.entry_time.isoformat() if t.entry_time else "",
            "exit_time": t.exit_time.isoformat() if t.exit_time else "",
            "entry": f"{t.entry:.2f}", "stop": f"{t.initial_stop:.2f}",
            "target": f"{t.target:.2f}", "exit_price": f"{t.exit_price:.2f}",
            "exit_reason": t.exit_reason, "units": f"{t.units:.2f}",
            "risk_pct": f"{t.risk_pct:.4%}",
            "risk_money": f"{t.risk_money:.2f}", "profit": f"{t.profit:.2f}",
            "r_multiple": f"{t.r_multiple:.2f}", "mfe_r": f"{t.mfe_r:.2f}",
            "mae_r": f"{t.mae_r:.2f}", "bars_open": t.bars_open,
            "spread_points": f"{t.spread_points:.0f}", "regime": t.regime,
            "session": t.session, "mgmt_mode": t.mgmt_mode,
            "reason": t.reason})

    def _reject_row(self, now, stage, sid, spread_points, reason):
        self._debug(f"rejected [{stage}] {sid}: {reason}")
        self.store.csv_append("rejections", {
            "time": now.isoformat(), "stage": stage, "strategy_id": sid,
            "regime": self._regime,
            "session": self.sessions.session_at(now).value,
            "spread_points": f"{spread_points:.0f}", "reason": reason})

    # ------------------------------------------------------------ heartbeat
    def _heartbeat(self, now, spread_points, equity):
        if self._last_heartbeat is not None and \
                (now - self._last_heartbeat) < timedelta(
                    minutes=self.cfg.heartbeat_minutes):
            return
        self._last_heartbeat = now
        floating = self._floating_pnl()
        lock = self.guard.lock_reason(equity, floating,
                                      self.sessions.session_at(now), now)
        day = self.guard.day
        daily_pl = ((day.realised + min(0.0, floating))
                    / day.start_equity * 100) if day and day.start_equity \
            else 0.0
        weekly_dd = 0.0
        if self.guard.week and self.guard.week.start_equity > 0:
            weekly_dd = max(0.0, (self.guard.week.start_equity
                                  - min(self.guard.week.min_equity, equity))
                            / self.guard.week.start_equity * 100)
        ranked = self.book.ranking(self.population)[:3]
        top = ", ".join(f"{p.sid}({sc:+.2f})" for p, sc in ranked)
        news_blocked, news_why = self.news.blackout(now)
        api.Print(
            f"HEARTBEAT day {self._day_index(now)}/{self.cfg.research_days} "
            f"| {now:%H:%M} UTC | bid {float(api.Symbol.Bid):.2f} spread "
            f"{spread_points:.0f}pt | regime {self._regime} | session "
            f"{self.sessions.session_at(now).value} | lock "
            f"{lock.value} | dayPL {daily_pl:+.2f}% weekDD {weekly_dd:.2f}% "
            f"| news {'BLOCKED: ' + news_why if news_blocked else 'clear'} "
            f"| shadows open {len(self.shadow.open)} pending "
            f"{len(self.shadow.pending)} | real "
            f"{'OPEN ' + self._real['strategy_id'] if self._real else 'flat'} "
            f"| top: {top}")
        self.store.csv_append("equity_history", {
            "time": now.isoformat(), "equity": f"{equity:.2f}",
            "balance": f"{float(api.Account.Balance):.2f}",
            "floating": f"{floating:.2f}",
            "daily_pl_pct": f"{daily_pl:.3f}",
            "weekly_dd_pct": f"{weekly_dd:.3f}"})

    # ------------------------------------------------------------ recovery
    def _replay_today_history(self, now):
        today = now.date()
        replayed = 0
        for i in range(int(api.History.Count)):
            h = api.History[i]
            try:
                if str(h.Label) != BOT_LABEL or \
                        str(h.SymbolName) != self._symbol_name:
                    continue
                closing = self._to_utc(h.ClosingTime)
            except Exception:
                continue
            if closing.date() != today:
                continue
            self.guard.register_close(float(h.NetProfit), now)
            if self.guard.day:
                self.guard.day.trades_opened += 1
            replayed += 1
        if replayed:
            api.Print(f"restart recovery: replayed {replayed} of today's "
                      f"closed V4 trades into the daily guard")

    def _adopt_open_position(self, saved, now):
        for i in range(int(api.Positions.Count)):
            pos = api.Positions[i]
            if str(pos.SymbolName) != self._symbol_name or \
                    str(pos.Label) != BOT_LABEL:
                continue
            sl = float(pos.StopLoss) if pos.StopLoss is not None else 0.0
            if sl <= 0:
                api.Print("adopted position has NO stop loss — closing for "
                          "safety")
                try:
                    api.ClosePosition(pos)
                except Exception as exc:
                    api.Print(f"protective close failed: {exc!r}")
                continue
            if saved and int(saved.get("position_id", -1)) == int(pos.Id):
                self._real = saved
                api.Print(f"restart recovery: resumed tracking of real "
                          f"position {int(pos.Id)} "
                          f"[{saved.get('strategy_id')}]")
            else:
                tp = float(pos.TakeProfit) if pos.TakeProfit is not None \
                    else 0.0
                entry = float(pos.EntryPrice)
                units = float(pos.VolumeInUnits)
                risk = abs(entry - sl) * units \
                    * self.spec.money_per_price_unit_per_unit()
                self._real = {
                    "trade_id": new_id("adopted"),
                    "position_id": int(pos.Id), "strategy_id": "ADOPTED",
                    "direction": "LONG" if str(pos.TradeType) == "Buy"
                    else "SHORT", "entry": entry, "stop": sl,
                    "initial_stop": sl, "target": tp, "units": units,
                    "risk_money": max(risk, 1e-9), "risk_pct": 0.0,
                    "entry_time": self._to_utc(pos.EntryTime).isoformat(),
                    "regime": self._regime, "session": "OFF_HOURS",
                    "reason": "adopted after restart",
                    "spread_points": 0.0, "mfe_r": 0.0, "mae_r": 0.0,
                    "bars_open": 0, "be_done": False, "partial_done": False,
                    "mgmt": {"mode": 0.0}, "mgmt_mode": "FULL_TP",
                    "tf": "M5"}
                api.Print(f"restart recovery: adopted unmatched position "
                          f"{int(pos.Id)} (managed as FULL_TP)")
            break

    def _save_state(self, now):
        self._last_state_save = now
        self.store.save_state({
            "research_start": self.research_start.isoformat(),
            "final_report_done": self.final_report_done,
            "population": [p.to_dict() for p in self.population],
            "learning": self.book.snapshot(),
            "shadow": self.shadow.snapshot(),
            "guard": self.guard.snapshot(),
            "open_real": self._real,
            "counters": {"mutation_serial": self._mutation_serial},
            "last_day_reported": self._last_day_reported,
            "saved_at": now.isoformat()})

    # ------------------------------------------------------------- utilities
    def _minutes_into(self, now, start_hour):
        delta = (now.hour - start_hour) * 60 + now.minute
        return delta if 0 <= delta <= 120 else None

    def _cost_price_units(self, spread_points):
        mpu = self.spec.money_per_price_unit_per_unit()
        return (spread_points + self.cfg.slippage_buffer_points) \
            * self.spec.point \
            + (2 * self.cfg.commission_per_unit / mpu if mpu > 0 else 0.0)

    def _symbol_is_gold(self, name):
        norm = "".join(ch for ch in name.upper() if ch.isalnum())
        allowed = {"".join(ch for ch in a.upper() if ch.isalnum())
                   for a in self.cfg.allowed_gold_symbols}
        return norm in allowed

    def _build_spec(self):
        sym = api.Symbol
        return CTraderSymbolSpec(
            name=self._symbol_name, digits=int(sym.Digits),
            tick_size=float(sym.TickSize), tick_value=float(sym.TickValue),
            pip_size=float(sym.PipSize), pip_value=float(sym.PipValue),
            volume_min=float(sym.VolumeInUnitsMin),
            volume_max=float(sym.VolumeInUnitsMax),
            volume_step=float(sym.VolumeInUnitsStep),
            spread_points=self._spread_points_raw(sym))

    def _spread_points_raw(self, sym):
        tick = float(sym.TickSize)
        return ((float(sym.Ask) - float(sym.Bid)) / tick) if tick > 0 else 0.0

    def _spread_points(self):
        return self._spread_points_raw(api.Symbol)

    def _to_utc(self, net_dt):
        t = datetime(int(net_dt.Year), int(net_dt.Month), int(net_dt.Day),
                     int(net_dt.Hour), int(net_dt.Minute),
                     int(net_dt.Second), tzinfo=UTC)
        return t - timedelta(hours=self.cfg.server_utc_offset_hours)

    def _now_utc(self):
        return self._to_utc(api.Server.Time)

    def _completed_candles(self, tf):
        bars = self._bars[tf]
        count = int(bars.Count)
        if count < 2:
            return []
        end = count - 1                    # exclude the forming bar
        start = max(0, end - self._windows[tf])
        out = []
        for i in range(start, end):
            out.append(Candle(
                time=self._to_utc(bars.OpenTimes[i]),
                open=float(bars.OpenPrices[i]),
                high=float(bars.HighPrices[i]),
                low=float(bars.LowPrices[i]),
                close=float(bars.ClosePrices[i]),
                volume=float(bars.TickVolumes[i])))
        return out

    def _symbol_positions_count(self):
        n = 0
        for i in range(int(api.Positions.Count)):
            if str(api.Positions[i].SymbolName) == self._symbol_name:
                n += 1
        return n

    def _find_position(self, position_id):
        for i in range(int(api.Positions.Count)):
            p = api.Positions[i]
            if str(p.SymbolName) == self._symbol_name \
                    and str(p.Label) == BOT_LABEL and int(p.Id) == position_id:
                return p
        return None

    def _has_pending_order(self):
        try:
            for i in range(int(api.PendingOrders.Count)):
                o = api.PendingOrders[i]
                if str(o.SymbolName) == self._symbol_name \
                        and str(o.Label) == BOT_LABEL:
                    return True
        except Exception:
            return False
        return False

    def _find_history(self, position_id):
        for i in range(int(api.History.Count) - 1, -1, -1):
            h = api.History[i]
            try:
                if int(h.PositionId) == position_id:
                    return h
            except Exception:
                continue
        return None

    def _floating_pnl(self):
        total = 0.0
        for i in range(int(api.Positions.Count)):
            p = api.Positions[i]
            if str(p.SymbolName) == self._symbol_name \
                    and str(p.Label) == BOT_LABEL:
                total += float(p.NetProfit)
        return total

    def _emergency_present(self):
        name = self.cfg.emergency_stop_file
        candidates = (self.store.directory,
                      os.path.join(os.path.expanduser("~"), "Documents",
                                   "XAUUSD_Adaptive_Bot_V4"),
                      os.getcwd())
        for base in candidates:
            if base and os.path.exists(os.path.join(base, name)):
                return True
        return self.cfg.emergency_stop

    def _debug(self, msg):
        if self.cfg.debug_logging:
            api.Print(f"[v4] {msg}")
