"""
============================================================================
XAUUSD_Adaptive_Bot_V3 -- native cTrader Algo Python cBot
                          (GOLD only, DEMO only, single self-contained file)
============================================================================

SINGLE-FILE BUILD: cTrader embeds and executes only the main Python file of
a cBot, so this file inlines the entire strategy package (market structure,
liquidity, supply/demand zones, order blocks, fair value gaps, regime,
M1 entry trigger, 0-100 setup scoring, position sizing, daily loss guard,
trade limits, session/news/spread filters, order safety, position
management and the CSV journal). It requires NO local folders, NO package
imports and NO third-party Python packages -- standard library only.

Strategy: M15 macro bias -> M5 decision layer -> M1 entry trigger, with
transparent scoring and strict risk limits (0.25%/trade hard cap, 1%/day
loss limit incl. floating, max 3 trades/day, max one open position,
structure-based stops that never widen, min RR 1.5).

SAFETY:
  * DEMO-ONLY: a live account prints "LIVE ACCOUNT BLOCKED" and stops the
    cBot immediately. There is no live-trading switch anywhere.
  * GOLD-ONLY: refuses to start on EURUSD or any symbol not in the gold
    allowlist (Config.allowed_gold_symbols).
  * Every order carries a stop loss and take profit, sized from the live
    broker volume rules (units, rounded DOWN); unverifiable data = no trade.
  * Decisions use COMPLETED candles only (the forming bar is always
    excluded) -- no look-ahead bias, no trading at startup.

News protection is schedule-based and manual (no live feed exists inside
cTrader Python): the NFP first-Friday rule is automatic; keep the
FOMC/CPI/speech date lists in the Config section up to date.

No profitability is claimed. Demo/backtest use only.
"""

import clr

clr.AddReference("cAlgo.API")

from cAlgo.API import *
from robot_wrapper import *

import csv
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple



#############################################################################
# CORE - DATA MODEL
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
# CORE - CONFIGURATION
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
# CORE - MATH HELPERS
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
# STRATEGY - MARKET STRUCTURE
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
# STRATEGY - LIQUIDITY
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
# STRATEGY - SUPPLY/DEMAND + ORDER BLOCKS
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
# STRATEGY - FAIR VALUE GAPS
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
# STRATEGY - MARKET REGIME
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
# STRATEGY - SETUP SCORING
#############################################################################

"""
Transparent 0-100 setup scoring with a full logged breakdown.

Scores are functions of market evidence only — never of recent P/L or
distance to the daily target.  The component weights are unchanged from the
legacy system so historical calibration (70 = B, 80 = A, 90 = A+) carries
over.  Note: with manual-only news protection the news component maxes at
3/5, so the practical maximum total is 98.

The M1 entry trigger and the spread check are hard GATES enforced by the
engine/order manager rather than score components: a setup without them is
rejected outright, which is stricter than any score bonus.
"""


class SetupScorer:

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

        # 15-minute bias alignment /15
        if (direction == Direction.LONG and htf_bias == TrendState.BULLISH) or \
           (direction == Direction.SHORT and htf_bias == TrendState.BEARISH):
            b.htf_alignment = 15.0
        elif htf_bias in (TrendState.RANGING, TrendState.UNDEFINED):
            b.htf_alignment = 8.0
        else:
            b.htf_alignment = 0.0     # countertrend

        # zone quality /15 (freshness, displacement, BOS link, overlaps)
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

        # 5-minute structure confirmation /15
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

        # news safety /5 (manual-only protection can never claim the full 5)
        if news_blocked:
            b.news_safety = 0.0
        elif news_protection_complete:
            b.news_safety = 5.0
        else:
            b.news_safety = 3.0    # manual/schedule-based protection

        # target quality / net RR /5
        if rr_tp1 >= 3.0:
            b.target_quality = 5.0
        elif rr_tp1 >= self.cfg.preferred_rr:
            b.target_quality = 4.0
        elif rr_tp1 >= self.cfg.min_rr:
            b.target_quality = 2.0
        if target_is_liquidity and b.target_quality > 0:
            b.target_quality = min(5.0, b.target_quality + 1.0)
        return b


#############################################################################
# STRATEGY - M1 ENTRY TRIGGER
#############################################################################

"""
1-minute entry trigger — the final gate before execution.

The M1 timeframe can NEVER create a trade by itself.  A setup must already
be fully confirmed on M15 (bias) and M5 (decision); this module then looks
at the last few completed M1 candles for precise entry evidence:

  * micro change of character / break of structure in the trade direction
  * a fresh liquidity sweep against the trade direction (stop-hunt fuel)
  * a rejection candle (dominant wick against the move, close in direction)
  * a fair-value-gap retest that held
  * an order-block mitigation that held
  * a strong displacement close in the trade direction
  * break-and-retest of the M5 confirmation level

If cfg.require_m1_trigger is False the gate always passes (logged as such);
that is the closest equivalent of the legacy MARKET_ON_CONFIRM entry mode.
"""


# imported for typing only (TFAnalysis lives in strategy_engine)
# a structural import cycle is avoided by duck-typing the tfa argument.


@dataclass
class TriggerResult:
    fired: bool
    kind: str
    note: str


class M1TriggerDetector:

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def check(self, m1_tfa, direction: Direction,
              confirmation_level: Optional[float] = None) -> TriggerResult:
        """m1_tfa: TFAnalysis of the M1 series (completed candles only)."""
        cfg = self.cfg
        if not cfg.require_m1_trigger:
            return TriggerResult(True, "DISABLED",
                                 "M1 trigger gate disabled in config")
        if m1_tfa is None or len(m1_tfa.candles) < cfg.atr_period + 5:
            return TriggerResult(False, "NO_DATA",
                                 "not enough completed M1 candles")
        n = len(m1_tfa.candles)
        window = cfg.m1_trigger_max_age
        last = m1_tfa.candles[-1]
        atr = m1_tfa.atr_now

        # 1. micro CHoCH / BOS / MSS in direction, recent
        for ev in reversed(m1_tfa.structure.events):
            if ev.index < n - window:
                break
            if ev.direction == direction and ev.kind in (
                    StructureEventKind.CHOCH, StructureEventKind.MSS,
                    StructureEventKind.BOS):
                return TriggerResult(True, f"M1_{ev.kind.value}",
                                     f"micro {ev.kind.value} at "
                                     f"{ev.broken_level:.2f}")

        # 2. fresh opposing-side liquidity sweep (sell-side for longs)
        for sv in reversed(m1_tfa.sweeps):
            if sv.index < n - window:
                break
            if sv.valid and sv.level.buy_side == (direction == Direction.SHORT):
                return TriggerResult(True, "M1_SWEEP",
                                     f"swept {sv.level.kind.value} at "
                                     f"{sv.level.price:.2f} and reclaimed")

        # 3. rejection candle: dominant wick against direction, close with it
        if last.range > 0:
            if direction == Direction.LONG and \
                    last.lower_wick >= 0.5 * last.range and \
                    last.close >= last.open:
                return TriggerResult(True, "M1_REJECTION",
                                     f"bullish rejection wick "
                                     f"{last.lower_wick:.2f}")
            if direction == Direction.SHORT and \
                    last.upper_wick >= 0.5 * last.range and \
                    last.close <= last.open:
                return TriggerResult(True, "M1_REJECTION",
                                     f"bearish rejection wick "
                                     f"{last.upper_wick:.2f}")

        # 4. FVG retest that held: last candle tapped a usable M1 FVG in
        #    direction and closed back in the trade direction
        for g in m1_tfa.fvgs:
            if g.direction != direction or g.state.value == "MITIGATED":
                continue
            tapped = last.low <= g.upper and last.high >= g.lower
            held = (last.close > g.upper if direction == Direction.LONG
                    else last.close < g.lower)
            if tapped and held:
                return TriggerResult(True, "M1_FVG_RETEST",
                                     f"FVG {g.lower:.2f}-{g.upper:.2f} "
                                     f"retested and held")

        # 5. order-block mitigation that held
        for b in m1_tfa.order_blocks:
            if b.direction != direction or b.invalidated:
                continue
            tapped = last.low <= b.upper and last.high >= b.lower
            held = (last.close > b.upper if direction == Direction.LONG
                    else last.close < b.lower)
            if tapped and held:
                return TriggerResult(True, "M1_OB_MITIGATION",
                                     f"OB {b.lower:.2f}-{b.upper:.2f} "
                                     f"mitigated and held")

        # 6. strong displacement close in direction
        if atr > 0 and is_displacement(last, atr, cfg.displacement_atr_mult,
                                       cfg.displacement_body_ratio):
            if (direction == Direction.LONG and last.bullish) or \
                    (direction == Direction.SHORT and last.bearish):
                return TriggerResult(True, "M1_DISPLACEMENT",
                                     f"displacement close, body "
                                     f"{last.body:.2f} vs ATR {atr:.2f}")

        # 7. break-and-retest of the M5 confirmation level
        if confirmation_level is not None and atr > 0:
            tol = 0.35 * atr
            recent = m1_tfa.candles[-window:]
            touched = any(
                (c.low <= confirmation_level + tol if direction == Direction.LONG
                 else c.high >= confirmation_level - tol)
                for c in recent)
            held = (last.close > confirmation_level if direction == Direction.LONG
                    else last.close < confirmation_level)
            closed_with = last.bullish if direction == Direction.LONG else last.bearish
            if touched and held and closed_with:
                return TriggerResult(True, "M1_BREAK_RETEST",
                                     f"retest of {confirmation_level:.2f} held")

        return TriggerResult(False, "NONE",
                             "no M1 trigger within the last "
                             f"{window} completed M1 candles")


#############################################################################
# STRATEGY - ENGINE (SIX MODELS + CONTEXT)
#############################################################################

"""
Strategy engine: builds the multi-timeframe AnalysisContext and runs the six
entry models, then scores and gates the best candidate into a Setup.

Timeframe roles are FIXED per the trading-system spec:
    M15 — macro bias & structure (HH/HL vs LH/LL, zones, premium/discount)
    M5  — decision layer (CHoCH/BOS confirmation, sweeps, displacement, FVG)
    M1  — entry trigger only (see entry_trigger.py; never trades alone)
    H1/H4/D1 — higher-timeframe zone context for the HTF_ZONE_REACTION model

All decisions use COMPLETED candles only.  The six models:
    1 TREND_CONTINUATION       bias + retrace into demand/supply + confirmation
    2 LIQUIDITY_SWEEP_REVERSAL sweep + displacement + CHoCH/MSS at zone/extreme
    3 BREAK_RETEST             displacement break, close beyond, retest holds
    4 RANGE_EXTREME            sweep beyond range edge, reclaim, CHoCH to mid
    5 SESSION_LIQUIDITY        Asian range swept by London/NY, reclaim+confirm
    6 HTF_ZONE_REACTION        D1/H4/H1 zone + LTF sweep + displacement + CHoCH

Counter-bias trades are rejected outright unless cfg.allow_reversals is True
(and even then they need cfg.countertrend_min_score).
"""


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
    time: object                      # datetime: close time of newest candle
    price: float                      # last decision-TF close
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
    stop_reason: str                  # human explanation of the anchor
    retest_level: Optional[float]     # M1 break-retest reference level
    note: str


FIXED_PLAN = TimeframePlan(
    bias_tf=Timeframe.M15, structure_tf=Timeframe.M15,
    decision_tf=Timeframe.M5, entry_tf=Timeframe.M1,
    management_tf=Timeframe.M5,
    reason="fixed per spec: M15 bias, M5 decision, M1 trigger")


class StrategyEngine:

    RECENT = 6      # candles: how recent confirmation must be
    ZONE_TOUCH_LOOKBACK = 8

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.scorer = SetupScorer(cfg)

    def _disp(self, candle: Candle, atr: float) -> bool:
        return is_displacement(candle, atr, self.cfg.displacement_atr_mult,
                               self.cfg.displacement_body_ratio)

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
        """MODEL 1: 15m bias + retracement into supply/demand + 5m confirmation."""
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
        disp = ev.displacement or self._disp(dec.last, dec.atr_now)
        if not disp and sweep is None:
            return None                       # need rejection evidence
        if not self._not_extended(ctx, ev, zone.mid if zone else None):
            return None
        # structural stop anchor
        if direction == Direction.SHORT:
            anchors = [(dec.last.high, "above last 5m candle high")]
            if sweep:
                anchors.append((sweep.extreme, "above the swept high"))
            if zone:
                anchors.append((zone.upper, "above the supply zone"))
            if dec.structure.protected_high:
                anchors.append((dec.structure.protected_high.price,
                                "above the protected lower-high"))
            stop_anchor, stop_reason = max(anchors, key=lambda a: a[0])
        else:
            anchors = [(dec.last.low, "below last 5m candle low")]
            if sweep:
                anchors.append((sweep.extreme, "below the swept low"))
            if zone:
                anchors.append((zone.lower, "below the demand zone"))
            if dec.structure.protected_low:
                anchors.append((dec.structure.protected_low.price,
                                "below the protected higher-low"))
            stop_anchor, stop_reason = min(anchors, key=lambda a: a[0])
        retest = ev.broken_level
        if fvg is not None:
            retest = fvg.midpoint
        elif ob is not None:
            retest = ob.upper if direction == Direction.SHORT else ob.lower
        return Candidate(SetupModel.TREND_CONTINUATION, direction, zone, sweep,
                         ev, fvg, ob, disp, stop_anchor, stop_reason, retest,
                         f"{bias.value} continuation off "
                         f"{'zone ' + zone.pattern.value if zone else 'confluence'}"
                         f" with {ev.kind.value}")

    def model_liquidity_sweep_reversal(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 2: zone/discount + sweep + displacement + 5m CHoCH/MSS."""
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
            # counter-bias reversals demand MSS or CHoCH WITH displacement
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
                stop_reason = "below the swept low"
            else:
                prot = dec.structure.last_confirmed_high
                if prot is None or prot.price > sweep.extreme:
                    prot_ok = prot is not None and prot.index > sweep.index
                else:
                    prot_ok = True
                if not prot_ok:
                    continue
                stop_anchor = max(sweep.extreme, dec.last.high)
                stop_reason = "above the swept high"
            if not self._not_extended(ctx, ev, sweep.level.price):
                continue
            fvg, ob = self._confluence(dec, direction, ev.broken_level,
                                       0.5 * dec.atr_now)
            disp = ev.displacement or sweep.displaced_away
            retest = fvg.midpoint if fvg else ev.broken_level
            return Candidate(SetupModel.LIQUIDITY_SWEEP_REVERSAL, direction,
                             zone, sweep, ev, fvg, ob, disp, stop_anchor,
                             stop_reason, retest,
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
                stop_reason = "below the retest low"
            else:
                stop_anchor = max(c.high for c in dec.candles[ev.index:n])
                stop_reason = "above the retest high"
            fvg, ob = self._confluence(dec, direction, level, 0.5 * dec.atr_now)
            sweep = self._recent_sweep(dec, buy_side=(direction == Direction.SHORT))
            return Candidate(SetupModel.BREAK_RETEST, direction, None, sweep,
                             ev, fvg, ob, True, stop_anchor, stop_reason, level,
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
        stop_reason = ("below the swept range low" if direction == Direction.LONG
                       else "above the swept range high")
        return Candidate(SetupModel.RANGE_EXTREME, direction, None, sweep, ev,
                         None, None, ev.displacement, sweep.extreme,
                         stop_reason, ev.broken_level,
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
                continue          # session reversals against 15m bias need MSS
            fvg, ob = self._confluence(dec, direction, ev.broken_level,
                                       0.5 * dec.atr_now)
            stop_reason = ("below the swept Asian low" if direction == Direction.LONG
                           else "above the swept Asian high")
            return Candidate(SetupModel.SESSION_LIQUIDITY, direction, None,
                             sweep, ev, fvg, ob, True, sweep.extreme,
                             stop_reason, ev.broken_level,
                             f"{ctx.session.value} sweep of Asian "
                             f"{'low' if direction == Direction.LONG else 'high'}")
        return None

    def model_htf_zone_reaction(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 6: D1/H4/H1 zone + LTF sweep + displacement + CHoCH."""
        entry_tfa = ctx.decision      # confirmation is read on the 5m layer
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
                    stop_reason = f"below the {htf.value} demand zone"
                else:
                    stop_anchor = max(zone.upper,
                                      sweep.extreme if sweep else zone.upper)
                    stop_reason = f"above the {htf.value} supply zone"
                zone.htf_aligned = True
                fvg, ob = self._confluence(entry_tfa, direction,
                                           ev.broken_level,
                                           0.5 * entry_tfa.atr_now)
                return Candidate(SetupModel.HTF_ZONE_REACTION, direction,
                                 zone, sweep, ev, fvg, ob,
                                 ev.displacement, stop_anchor, stop_reason,
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
            except Exception as exc:            # a crashed model must never
                if reject_cb:                   # take the bot down
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

        # ---- 15m bias gate: reversals must be explicitly enabled ----------
        htf_bias = self._htf_bias(ctx)
        countertrend = (direction == Direction.LONG and htf_bias == TrendState.BEARISH) \
            or (direction == Direction.SHORT and htf_bias == TrendState.BULLISH)
        if countertrend and not cfg.allow_reversals:
            reject("bias", f"{direction.value} against 15m bias "
                   f"{htf_bias.value} and allow_reversals is False")
            return None

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

        entry_price = price               # market entry after M1 trigger

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
        target_reason = f"opposing {usable[0].kind.value} at {tp1:.2f}"
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

        if rr1 < cfg.min_rr:
            reject("rr", f"net RR to TP1 {rr1:.2f} < required {cfg.min_rr:.2f}")
            return None

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
            runner_target=runner, score=score,
            grade=grade, breakdown=breakdown, tf_plan=ctx.tf_plan,
            regime=ctx.regime.regime, session=ctx.session,
            htf_bias=htf_bias, zone=cand.zone, sweep=cand.sweep,
            structure_event=cand.structure_event, fvg=cand.fvg,
            order_block=cand.order_block, atr=atr,
            spread_points=ctx.spread_points, reason=cand.note,
            stop_reason=f"{cand.stop_reason} + {cfg.stop_buffer_atr} ATR buffer",
            target_reason=target_reason)


class ContextBuilder:
    """Builds an AnalysisContext from per-timeframe COMPLETED candle series
    (supplied by the main cBot from native cTrader Bars)."""

    def __init__(self, cfg: Config, engine: StrategyEngine,
                 sessions, news):
        self.cfg = cfg
        self.engine = engine
        self.sessions = sessions
        self.news = news

    def build(self, series: Dict[Timeframe, List[Candle]],
              spread_points: float, point: float,
              now) -> Optional[AnalysisContext]:
        cfg = self.cfg
        plan = FIXED_PLAN
        needed = {plan.bias_tf, plan.structure_tf, plan.decision_tf,
                  plan.entry_tf, plan.management_tf,
                  Timeframe.H1, Timeframe.H4, Timeframe.D1}

        news_blocked, news_reason = self.news.blackout(now)
        session = self.sessions.session_at(now)
        day = now.date()
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
        if plan.decision_tf not in tfs or plan.structure_tf not in tfs \
                or plan.bias_tf not in tfs:
            return None
        if cfg.strict_mode and plan.entry_tf not in tfs \
                and cfg.require_m1_trigger:
            return None       # strict: no M1 data => no trading

        # regime on the structure timeframe (M15)
        sfa = tfs[plan.structure_tf]
        regime = MarketRegimeDetector(cfg).classify(
            sfa.candles, sfa.structure, spread_points, news_blocked)
        return AnalysisContext(
            time=now, price=series[plan.decision_tf][-1].close,
            spread_points=spread_points, point=point, tf_plan=plan,
            regime=regime, session=session, news_blocked=news_blocked,
            news_reason=news_reason,
            news_complete=self.news.protection_complete(),
            tfs=tfs, asian_range=self.sessions.asian_range(day))


#############################################################################
# FILTERS - SESSIONS
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
# FILTERS - NEWS PROTECTION
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
# FILTERS - SPREAD
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
# RISK - POSITION SIZING
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
# RISK - DAILY LOSS GUARD
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
# RISK - TRADE LIMITS
#############################################################################

"""
Trade-count limits and duplicate-entry prevention.

The final can_open() combines every account-protection rule into a single
yes/no with a reason.  All checks are pure reads — nothing here ever
increases risk.  There is no martingale, no grid, no averaging down, no
recovery mode anywhere in this codebase.
"""


class TradeLimits:

    def __init__(self, cfg: Config, guard: DailyLossGuard):
        self.cfg = cfg
        self.guard = guard

    def can_open(self, equity: float, open_positions: int,
                 open_risk_money: float, new_risk_money: float,
                 unrealised: float = 0.0,
                 session: Optional[SessionName] = None) -> Tuple[bool, str]:
        cfg = self.cfg
        lock = self.guard.lock_reason(equity, unrealised, session)
        if lock != LockReason.NONE:
            return False, f"lock active: {lock.value}"
        if open_positions >= cfg.max_positions:
            return False, (f"max positions ({cfg.max_positions}) reached — "
                           f"one open GOLD position rule")
        if equity <= 0:
            return False, "equity not verifiable"
        if new_risk_money <= 0:
            return False, "new trade has no measurable risk (sizing failed)"
        max_total = equity * cfg.max_risk_per_trade * cfg.max_positions
        if open_risk_money + new_risk_money > max_total + 1e-9:
            return False, (f"combined open risk "
                           f"{(open_risk_money + new_risk_money) / equity:.2%} "
                           f"> allowed {max_total / equity:.2%}")
        return True, "ok"


#############################################################################
# EXECUTION - ORDER SAFETY
#############################################################################

"""
Order safety: the full pre-order checklist from the spec, evaluated as pure
logic.  The main cBot gathers the live facts (account type, symbol state,
market open, spread, open positions, news, locks), passes them in, and only
sends the order if EVERY check passes.  The actual ExecuteMarketOrder call
lives in the main file — this module never touches the cAlgo API, which is
what makes the checklist testable outside cTrader.

Also enforces the no-spam rule: after a failed order request the manager
imposes a cooldown before any new order may be attempted.
"""


@dataclass
class OrderFacts:
    """Everything the checklist needs, gathered by the main cBot."""
    is_demo_account: bool
    symbol_is_gold: bool
    market_open: bool
    spread_points: float
    spread_ok: bool
    open_positions_count: int
    has_pending_bot_order: bool       # duplicate prevention
    news_blocked: bool
    news_reason: str
    lock: LockReason
    equity: float
    session_allowed: bool
    session_reason: str


@dataclass
class Preflight:
    ok: bool
    checks: List[str] = field(default_factory=list)   # "PASS/FAIL — detail"
    reason: str = ""                                  # first failure


class OrderManager:

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cooldown_until: Optional[datetime] = None
        self._last_failure: str = ""

    # ---- failure cooldown (no order spam) ---------------------------------
    def order_failed(self, now: datetime, broker_message: str) -> None:
        self._cooldown_until = now + timedelta(
            minutes=self.cfg.order_fail_cooldown_min)
        self._last_failure = broker_message

    def order_succeeded(self) -> None:
        self._cooldown_until = None
        self._last_failure = ""

    def in_cooldown(self, now: datetime) -> bool:
        return self._cooldown_until is not None and now < self._cooldown_until

    # ---- the checklist ------------------------------------------------------
    def preflight(self, now: datetime, setup: Setup, sizing: SizingResult,
                  facts: OrderFacts) -> Preflight:
        cfg = self.cfg
        checks: List[str] = []
        failures: List[str] = []

        def check(name: str, passed: bool, detail: str) -> None:
            checks.append(f"{'PASS' if passed else 'FAIL'} — {name}: {detail}")
            if not passed:
                failures.append(f"{name}: {detail}")

        check("demo account", facts.is_demo_account,
              "account is demo" if facts.is_demo_account
              else "LIVE ACCOUNT — trading refused")
        check("gold symbol", facts.symbol_is_gold,
              "symbol verified as gold" if facts.symbol_is_gold
              else "symbol is not an approved gold alias")
        check("market open", facts.market_open,
              "market open" if facts.market_open else "market closed")
        check("spread", facts.spread_ok,
              f"{facts.spread_points:.0f} pts (max {cfg.max_spread_points:.0f})")
        check("stop loss present", setup.stop_price > 0
              and setup.stop_price != setup.entry_price,
              f"SL {setup.stop_price:.2f}")
        check("take profit present", setup.tp1 > 0
              and setup.tp1 != setup.entry_price,
              f"TP {setup.tp1:.2f}")
        sl_side_ok = (setup.stop_price < setup.entry_price < setup.tp1
                      if setup.direction.value == "LONG"
                      else setup.tp1 < setup.entry_price < setup.stop_price)
        check("SL/TP on correct sides", sl_side_ok,
              f"entry {setup.entry_price:.2f} SL {setup.stop_price:.2f} "
              f"TP {setup.tp1:.2f} ({setup.direction.value})")
        check("volume valid", not sizing.rejected and sizing.volume_units > 0,
              f"{sizing.volume_units} units" if not sizing.rejected
              else sizing.reason)
        risk_ok = (facts.equity > 0 and not sizing.rejected
                   and sizing.risk_money
                   <= facts.equity * cfg.max_risk_per_trade * 1.0001)
        check("risk below maximum", risk_ok,
              f"{sizing.risk_money:.2f} "
              f"({sizing.risk_fraction_actual:.2%} of equity, "
              f"max {cfg.max_risk_per_trade:.2%})" if not sizing.rejected
              else sizing.reason)
        check("no daily lock", facts.lock == LockReason.NONE,
              "no lock" if facts.lock == LockReason.NONE
              else f"lock: {facts.lock.value}")
        check("no other GOLD position", facts.open_positions_count == 0,
              f"{facts.open_positions_count} open position(s)")
        check("no duplicate pending order", not facts.has_pending_bot_order,
              "none" if not facts.has_pending_bot_order
              else "a bot order is already pending")
        check("news clear", not facts.news_blocked,
              "clear" if not facts.news_blocked else facts.news_reason)
        check("session allowed", facts.session_allowed, facts.session_reason)
        check("score above minimum", setup.score >= cfg.min_score,
              f"{setup.score:.1f} >= {cfg.min_score:.1f}")
        cooled = not self.in_cooldown(now)
        check("no failure cooldown", cooled,
              "clear" if cooled else
              f"cooling down after order failure: {self._last_failure}")

        return Preflight(ok=not failures, checks=checks,
                         reason=failures[0] if failures else "")


#############################################################################
# EXECUTION - POSITION MANAGEMENT
#############################################################################

"""
Open-position management: break-even, partial take-profit and structural
trailing — ALL DISABLED BY DEFAULT until tested (see config.py), plus the
always-on safety exits (time stop, pre-weekend flat).

Hard rules enforced here:
  * a stop is NEVER widened and NEVER removed;
  * break-even only with evidence (>= breakeven_r AND, when configured,
    a confirmed protected swing in the profit direction);
  * trailing follows confirmed management-TF swings, not every tick;
  * stops are kept off obvious liquidity by a small ATR offset.

This module produces ManagementAction instructions; the main cBot applies
them through the cTrader API (ModifyPosition / ClosePosition).
"""


@dataclass
class ManagementAction:
    """One instruction produced by PositionManager for the main cBot."""
    kind: str                 # "MOVE_STOP" | "PARTIAL_CLOSE" | "CLOSE"
    trade: Trade
    price: float = 0.0        # new stop or close reference
    volume_units: float = 0.0 # for partial closes
    reason: ExitReason = ExitReason.MANUAL
    note: str = ""


class PositionManager:

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def manage(self, trade: Trade, mgmt: TFAnalysis,
               now: datetime, spec: CTraderSymbolSpec,
               weekend_flat: bool = False) -> List[ManagementAction]:
        cfg = self.cfg
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
        if weekend_flat and not cfg.weekend_hold_allowed:
            actions.append(ManagementAction("CLOSE", trade, price=price,
                                            reason=ExitReason.WEEKEND_EXIT,
                                            note="pre-weekend flat"))
            return actions
        if trade.entry_time and \
                (now - trade.entry_time) >= timedelta(hours=cfg.time_exit_hours):
            actions.append(ManagementAction("CLOSE", trade, price=price,
                                            reason=ExitReason.TIME_EXIT,
                                            note=f"open > {cfg.time_exit_hours}h"))
            return actions
        if trade.bars_open >= cfg.max_bars_no_progress and trade.mfe < 0.2:
            actions.append(ManagementAction("CLOSE", trade, price=price,
                                            reason=ExitReason.TIME_EXIT,
                                            note="no progress time stop"))
            return actions

        # ---- break-even (OFF by default) ------------------------------------
        if cfg.breakeven_enabled and not trade.breakeven_done \
                and r_now >= cfg.breakeven_r:
            structure_ok = True
            if cfg.breakeven_needs_structure:
                structure_ok = self._protected_swing_in_profit(trade, mgmt)
            if structure_ok:
                costs = (spec.spread_points + cfg.slippage_buffer_points) \
                    * spec.point
                be = trade.entry_price + d.sign * costs
                if self._tightens(trade, be):
                    actions.append(ManagementAction(
                        "MOVE_STOP", trade, price=be,
                        reason=ExitReason.BREAK_EVEN,
                        note=f"BE at +{r_now:.2f}R with structure"))
                    trade.breakeven_done = True

        # ---- partial profit (OFF by default) ---------------------------------
        if cfg.partial_tp_enabled and not trade.partial_done \
                and r_now >= cfg.partial_r \
                and trade.volume_units > spec.volume_min:
            vol = spec.round_volume_down(
                trade.initial_volume_units * cfg.partial_fraction)
            if vol >= spec.volume_min and vol < trade.volume_units:
                actions.append(ManagementAction(
                    "PARTIAL_CLOSE", trade, price=price, volume_units=vol,
                    reason=ExitReason.PARTIAL_TP,
                    note=f"partial {cfg.partial_fraction:.0%} at +{r_now:.2f}R"))
                trade.partial_done = True

        # ---- structural trailing (OFF by default) -----------------------------
        if cfg.trailing_enabled:
            trail = self._structural_trail(trade, mgmt, cfg.trail_atr_mult)
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


#############################################################################
# JOURNAL - CSV
#############################################################################

"""
CSV journal: every accepted/rejected setup and every completed trade,
written best-effort to a local folder.

Default location: <home>/Documents/XAUUSD_Adaptive_Bot/
    setups_YYYY-MM.csv   one row per evaluated setup (accepted or rejected)
    trades_YYYY-MM.csv   one row per completed trade

File IO on macOS can be sandboxed depending on how cTrader was installed;
if a write fails the journal disables itself for the session with a single
log line and the bot keeps running on cTrader's own log alone.  No secrets
are ever written — only market/trade data.
"""


SETUP_FIELDS = [
    "time", "symbol", "decision", "rejection_reason", "model", "direction",
    "timeframes", "session", "regime", "htf_bias", "premium_discount",
    "zone", "liquidity_event", "m5_confirmation", "m1_trigger",
    "news_status", "spread_points", "score", "grade",
    "score_htf", "score_zone", "score_sweep", "score_structure",
    "score_displacement", "score_confluence", "score_pd", "score_session",
    "score_news", "score_target",
    "risk_pct", "volume_units", "entry", "stop_loss", "take_profit",
    "rr_tp1", "stop_reason", "target_reason",
]

TRADE_FIELDS = [
    "trade_id", "position_id", "direction", "entry_time", "exit_time",
    "entry_price", "exit_price", "stop_loss", "take_profit", "volume_units",
    "profit", "r_multiple", "mfe_r", "mae_r", "setup_score", "setup_model",
    "exit_reason",
]


class TradeJournal:

    def __init__(self, cfg: Config, log: Callable[[str], None]):
        self.cfg = cfg
        self.log = log
        self.enabled = cfg.journal_enabled
        self.directory: Optional[str] = None
        if self.enabled:
            self.directory = self._prepare_dir()
            if self.directory is None:
                self.enabled = False

    def _prepare_dir(self) -> Optional[str]:
        base = self.cfg.journal_dir.strip()
        candidates = []
        if base:
            candidates.append(base)
        home = os.path.expanduser("~")
        candidates.append(os.path.join(home, "Documents", "XAUUSD_Adaptive_Bot"))
        candidates.append(os.path.join(home, "XAUUSD_Adaptive_Bot"))
        candidates.append(os.path.join(os.getcwd(), "XAUUSD_Adaptive_Bot_journal"))
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
        self.log("JOURNAL: no writable directory found — CSV journal "
                 "disabled for this session (cTrader log still records "
                 "everything)")
        return None

    def _append(self, filename: str, fields: List[str], row: dict) -> None:
        if not self.enabled or self.directory is None:
            return
        path = os.path.join(self.directory, filename)
        try:
            fresh = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields,
                                        extrasaction="ignore")
                if fresh:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:
            self.enabled = False
            self.log(f"JOURNAL: write failed ({exc}) — CSV journal disabled "
                     f"for this session")

    # ------------------------------------------------------------------ setups
    def record_setup(self, now: datetime, symbol: str, setup: Optional[Setup],
                     accepted: bool, rejection_reason: str = "",
                     spread_points: float = 0.0, session: str = "",
                     news_status: str = "", risk_pct: float = 0.0,
                     volume_units: float = 0.0, m1_trigger: str = "",
                     model: str = "", direction: str = "",
                     regime: str = "", htf_bias: str = "",
                     pd_zone: str = "") -> None:
        row = {
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "decision": "ACCEPTED" if accepted else "REJECTED",
            "rejection_reason": rejection_reason,
            "timeframes": "M15/M5/M1",
            "session": session,
            "news_status": news_status,
            "spread_points": f"{spread_points:.1f}",
            "risk_pct": f"{risk_pct:.4%}" if risk_pct else "",
            "volume_units": volume_units or "",
            "m1_trigger": m1_trigger,
            "model": model, "direction": direction,
            "regime": regime, "htf_bias": htf_bias,
            "premium_discount": pd_zone,
        }
        if setup is not None:
            b = setup.breakdown
            row.update({
                "model": setup.model.value,
                "direction": setup.direction.value,
                "regime": setup.regime.value,
                "htf_bias": setup.htf_bias.value,
                "zone": (f"{setup.zone.kind.value} {setup.zone.pattern.value} "
                         f"{setup.zone.lower:.2f}-{setup.zone.upper:.2f} "
                         f"fresh {setup.zone.freshness:.2f}"
                         if setup.zone else ""),
                "liquidity_event": (f"sweep {setup.sweep.level.kind.value} @ "
                                    f"{setup.sweep.level.price:.2f}"
                                    if setup.sweep else ""),
                "m5_confirmation": (setup.structure_event.kind.value
                                    if setup.structure_event else ""),
                "m1_trigger": setup.m1_trigger or m1_trigger,
                "score": f"{setup.score:.1f}",
                "grade": setup.grade.value,
                "score_htf": b.htf_alignment, "score_zone": b.zone_quality,
                "score_sweep": b.liquidity_sweep,
                "score_structure": b.structure_confirmation,
                "score_displacement": b.displacement,
                "score_confluence": b.confluence,
                "score_pd": b.premium_discount,
                "score_session": b.session_quality,
                "score_news": b.news_safety,
                "score_target": b.target_quality,
                "entry": f"{setup.entry_price:.2f}",
                "stop_loss": f"{setup.stop_price:.2f}",
                "take_profit": f"{setup.tp1:.2f}",
                "rr_tp1": f"{setup.rr_to(setup.tp1):.2f}",
                "stop_reason": setup.stop_reason,
                "target_reason": setup.target_reason,
            })
        self._append(f"setups_{now:%Y-%m}.csv", SETUP_FIELDS, row)

    # ------------------------------------------------------------------ trades
    def record_trade(self, trade: Trade) -> None:
        row = {
            "trade_id": trade.trade_id,
            "position_id": trade.position_id,
            "direction": trade.direction.value,
            "entry_time": (trade.entry_time.strftime("%Y-%m-%d %H:%M:%S")
                           if trade.entry_time else ""),
            "exit_time": (trade.exit_time.strftime("%Y-%m-%d %H:%M:%S")
                          if trade.exit_time else ""),
            "entry_price": f"{trade.entry_price:.2f}",
            "exit_price": f"{trade.exit_price:.2f}",
            "stop_loss": f"{trade.stop_price:.2f}",
            "take_profit": f"{trade.tp1:.2f}",
            "volume_units": trade.initial_volume_units,
            "profit": f"{trade.profit:.2f}",
            "r_multiple": f"{trade.r_multiple():.2f}",
            "mfe_r": f"{trade.mfe:.2f}",
            "mae_r": f"{trade.mae:.2f}",
            "setup_score": f"{trade.setup.score:.1f}",
            "setup_model": trade.setup.model.value,
            "exit_reason": trade.exit_reason.value if trade.exit_reason else "",
        }
        when = trade.exit_time or trade.entry_time or datetime.now()
        self._append(f"trades_{when:%Y-%m}.csv", TRADE_FIELDS, row)


#############################################################################
# THE cBOT (the only section that talks to the cTrader API)
#############################################################################

BOT_LABEL = "XAUUSD_Adaptive_Bot_V3"

# minutes an armed setup stays valid while waiting for its M1 trigger
ARMED_SETUP_VALIDITY_MIN = 15


class XAUUSD_Adaptive_Bot_V3(object):

    # ------------------------------------------------------------ lifecycle
    def on_start(self):
        self._fatal = False
        self._tracked = None            # bot-side Trade record of open position
        self._armed = None              # (Setup, expiry_datetime) awaiting M1 trigger
        self._last_m1_open = None       # duplicate-tick / new-bar guard
        self._last_m5_bucket = None     # decision de-duplication
        self._last_mgmt_bucket = None
        self._last_lock_logged = None

        self.cfg = Config()
        self._apply_ui_parameter_overrides()

        validator = ConfigValidator(self.cfg)
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

        # ---- DEMO-ONLY SAFETY LOCK (no live switch exists) -----------------
        if bool(api.Account.IsLive):
            api.Print("LIVE ACCOUNT BLOCKED")
            api.Print("This bot is demo-only. Connect a Skilling DEMO "
                      "account and restart.")
            self._fatal = True
            api.Stop()
            return
        api.Print("account check: DEMO account confirmed")

        # ---- GOLD-ONLY symbol verification ----------------------------------
        self._symbol_name = str(api.SymbolName)
        if not self._symbol_is_gold(self._symbol_name):
            api.Print(f"SYMBOL BLOCKED: '{self._symbol_name}' is not an "
                      f"approved gold symbol {self.cfg.allowed_gold_symbols}")
            api.Print("Attach this cBot to your broker's GOLD chart "
                      "(Skilling: XAUUSD). EURUSD and all non-gold symbols "
                      "are refused.")
            self._fatal = True
            api.Stop()
            return
        api.Print(f"symbol check: {self._symbol_name} accepted as GOLD")

        # ---- symbol specification from the live platform --------------------
        self.spec = self._build_spec()
        ok, why = self.spec.valid()
        if not ok:
            api.Print(f"SYMBOL SPEC UNVERIFIABLE: {why} — stopping (strict)")
            self._fatal = True
            api.Stop()
            return
        mpu = self.spec.money_per_price_unit_per_unit()
        api.Print(f"symbol spec: tick {self.spec.tick_size} | pip "
                  f"{self.spec.pip_size} | 1 unit per 1.0 move = "
                  f"{mpu:.4f} {str(api.Account.Currency)} | volume "
                  f"min/step/max = {self.spec.volume_min}/"
                  f"{self.spec.volume_step}/{self.spec.volume_max} units")
        if self.cfg.strict_mode and not (0.1 <= mpu <= 10.0):
            api.Print("STRICT MODE: per-unit value looks implausible for "
                      "gold — refusing to trade until verified. Compare the "
                      "printed value with your account currency and, if it "
                      "is genuinely correct, relax strict_mode in config.py.")
            self._fatal = True
            api.Stop()
            return

        # ---- strategy stack --------------------------------------------------
        self.engine = StrategyEngine(self.cfg)
        self.sessions = SessionManager(self.cfg)
        self.news = NewsFilter(self.cfg, UTC)
        self.builder = ContextBuilder(self.cfg, self.engine, self.sessions,
                                      self.news)
        self.spread_filter = SpreadFilter(self.cfg)
        self.sizer = PositionSizer(self.cfg)
        self.guard = DailyLossGuard(self.cfg)
        self.limits = TradeLimits(self.cfg, self.guard)
        self.orders = OrderManager(self.cfg)
        self.pos_manager = PositionManager(self.cfg)
        self.trigger = M1TriggerDetector(self.cfg)
        self.journal = TradeJournal(self.cfg, lambda m: api.Print(m))

        for bad in self.news.malformed_entries():
            api.Print(f"NEWS CONFIG: ignoring malformed entry: {bad}")
        api.Print("NEWS PROTECTION: schedule-based and manual only (no live "
                  "feed exists inside cTrader Python). NFP first-Friday rule "
                  f"{'ON' if self.cfg.block_nfp else 'OFF'}; configured "
                  f"events: {len(self.news.upcoming(self._now_utc(), 24*365))} "
                  "— keep fomc_events/cpi_events in config.py up to date.")

        # ---- market data ------------------------------------------------------
        self._tf_map = {
            Timeframe.M1: TimeFrame.Minute,
            Timeframe.M5: TimeFrame.Minute5,
            Timeframe.M15: TimeFrame.Minute15,
            Timeframe.H1: TimeFrame.Hour,
            Timeframe.H4: TimeFrame.Hour4,
            Timeframe.D1: TimeFrame.Daily,
        }
        self._windows = {
            Timeframe.M1: self.cfg.window_m1,
            Timeframe.M5: self.cfg.window_m5,
            Timeframe.M15: self.cfg.window_m15,
            Timeframe.H1: self.cfg.window_h1,
            Timeframe.H4: self.cfg.window_h4,
            Timeframe.D1: self.cfg.window_d1,
        }
        self._bars = {}
        for tf, ctf in self._tf_map.items():
            self._bars[tf] = api.MarketData.GetBars(ctf)

        # rebuild session ranges from recent M5 history (~2 days)
        m5_hist = self._completed_candles(Timeframe.M5)
        self.sessions.rebuild_from(m5_hist)

        # daily guard: initialise and REPLAY today's bot history so a
        # mid-day restart cannot bypass the daily lock
        now = self._now_utc()
        equity = float(api.Account.Equity)
        balance = float(api.Account.Balance)
        self.guard.roll(now, equity, balance)
        self._restore_today_from_history(now)
        api.Print(f"daily guard: {self.guard.describe(equity)}")

        # adopt an already-open bot position after a restart (never trade
        # around it blindly)
        self._adopt_open_position(now)

        # never trade on the bucket that was already in progress at startup
        m1 = self._completed_candles(Timeframe.M1)
        if m1:
            self._last_m1_open = m1[-1].time
            self._last_m5_bucket = tf_bucket_start(m1[-1].time, Timeframe.M5)
            self._last_mgmt_bucket = self._last_m5_bucket

        api.Print(f"{BOT_LABEL} started on {self._symbol_name} | DEMO | "
                  f"M15 bias / M5 decision / M1 trigger | risk "
                  f"{self.cfg.max_risk_per_trade:.2%}/trade, daily loss "
                  f"limit {self.cfg.max_daily_loss:.0%}, max "
                  f"{self.cfg.max_trades_per_day} trades/day | journal: "
                  f"{self.journal.directory or 'cTrader log only'}")
        api.Print("first evaluation happens on the NEXT completed M5 candle "
                  "— the bot never trades at startup")

    def on_tick(self):
        if self._fatal:
            return
        try:
            self._tick()
        except Exception as exc:
            # one bad tick must never kill protection of an open position
            api.Print(f"ERROR in tick processing: {exc!r}")

    def on_stop(self):
        if self._fatal:
            return
        api.Print(f"{BOT_LABEL} stopping. "
                  f"{self.guard.describe(float(api.Account.Equity))}")
        if self._tracked is not None:
            api.Print(f"NOTE: position {self._tracked.position_id} remains "
                      f"open with SL {self._tracked.stop_price:.2f} / TP "
                      f"{self._tracked.tp1:.2f} held on the broker side.")
        if self.journal.directory:
            api.Print(f"CSV journal: {self.journal.directory}")

    # ------------------------------------------------------------ main tick
    def _tick(self):
        m1_bars = self._bars[Timeframe.M1]
        if m1_bars.Count < 3:
            return
        last_completed_open = self._to_utc(m1_bars.OpenTimes[m1_bars.Count - 2])
        if self._last_m1_open is not None \
                and last_completed_open == self._last_m1_open:
            return                       # no new completed M1 bar yet
        self._last_m1_open = last_completed_open

        m1 = self._completed_candles(Timeframe.M1)
        if not m1:
            return
        candle = m1[-1]
        now = candle.time + timedelta(minutes=1)   # close time of that candle

        # session ranges + day/week roll + emergency stop state
        self.sessions.update_ranges(candle)
        equity = float(api.Account.Equity)
        balance = float(api.Account.Balance)
        new_day, _ = self.guard.roll(now, equity, balance)
        if new_day:
            api.Print(f"new trading day: {self.guard.describe(equity)}")
        self.guard.emergency = self._emergency_file_present()

        # reconcile a position the broker closed (SL/TP hit etc.)
        self._reconcile_closed(now)

        # excursion tracking for the journal
        self._update_excursions(candle)

        m5_bucket = tf_bucket_start(candle.time, Timeframe.M5)

        # manage the open position on completed M5 candles
        if self._tracked is not None and m5_bucket != self._last_mgmt_bucket:
            self._last_mgmt_bucket = m5_bucket
            self._manage_position(now)

        # M1 trigger check for an armed setup (every completed M1 candle)
        if self._armed is not None and self._tracked is None:
            self._try_trigger(now, m1)

        # decision layer on completed M5 candles
        if m5_bucket != self._last_m5_bucket:
            self._last_m5_bucket = m5_bucket
            self._evaluate(now)

    # ------------------------------------------------------------ decision
    def _evaluate(self, now):
        cfg = self.cfg
        if self._tracked is not None:
            return                          # one position rule — nothing new
        spread_points = self._spread_points()

        series = {tf: self._completed_candles(tf) for tf in self._tf_map}
        ctx = self.builder.build(series, spread_points, self.spec.point, now)
        if ctx is None:
            self._debug("analysis context unavailable (not enough history)")
            return

        if cfg.debug_logging:
            api.Print(f"plan {ctx.tf_plan.as_dict()} | regime "
                      f"{ctx.regime.regime.value} ({ctx.regime.reason}) | "
                      f"session {ctx.session.value} | spread "
                      f"{spread_points:.0f} pts")

        # hard gates before any model runs
        equity = float(api.Account.Equity)
        unreal = self._floating_pnl()
        lock = self.guard.lock_reason(equity, unreal, ctx.session)
        if lock != LockReason.NONE:
            if self.guard.lock_changed(lock):
                api.Print(f"NO NEW ENTRIES — lock active: {lock.value} | "
                          f"{self.guard.describe(equity)}")
            self._armed = None
            return
        self.guard.lock_changed(lock)      # records the unlocked state

        allowed, window_why = self.sessions.entry_window_check(now)
        if not allowed:
            self._debug(f"entry window closed: {window_why}")
            self._armed = None
            return
        if ctx.news_blocked:
            api.Print(f"NEWS PROTECTION: no entries — {ctx.news_reason}")
            self.journal.record_setup(
                now, self._symbol_name, None, accepted=False,
                rejection_reason=f"news blackout: {ctx.news_reason}",
                spread_points=spread_points, session=ctx.session.value,
                news_status="BLOCKED")
            self._armed = None
            return
        spread_ok, spread_why = self.spread_filter.check(spread_points)
        if not spread_ok:
            self._debug(f"spread gate: {spread_why}")
            self._armed = None
            return

        # cost model in price units for net-RR checks
        mpu = self.spec.money_per_price_unit_per_unit()
        cost = (spread_points + cfg.slippage_buffer_points) * self.spec.point \
            + (cfg.commission_per_unit / mpu if mpu > 0 else 0.0)

        def reject_cb(model, stage, reason):
            self._debug(f"candidate rejected [{model}/{stage}]: {reason}")
            if stage in ("score", "rr", "bias", "stop", "target"):
                self.journal.record_setup(
                    now, self._symbol_name, None, accepted=False,
                    rejection_reason=f"[{model}/{stage}] {reason}",
                    spread_points=spread_points, session=ctx.session.value,
                    news_status="CLEAR", model=model,
                    regime=ctx.regime.regime.value)

        setup = self.engine.evaluate(ctx, cost, reject_cb)
        if setup is None:
            self._armed = None
            return

        # arm the setup and wait for the 1-minute trigger
        expiry = now + timedelta(minutes=ARMED_SETUP_VALIDITY_MIN)
        self._armed = (setup, expiry)
        api.Print(f"SETUP ARMED: {setup.model.value} {setup.direction.value} "
                  f"{setup.grade.value} score {setup.score:.1f} | entry ref "
                  f"{setup.entry_price:.2f} SL {setup.stop_price:.2f} "
                  f"({setup.stop_reason}) TP {setup.tp1:.2f} "
                  f"({setup.target_reason}) | waiting for M1 trigger "
                  f"(valid until {expiry:%H:%M} UTC)")
        for line in setup.breakdown.lines():
            api.Print(line)
        api.Print(f"  reason: {setup.reason}")

    # ------------------------------------------------------- trigger & entry
    def _try_trigger(self, now, m1_candles):
        setup, expiry = self._armed
        if now > expiry:
            api.Print(f"SETUP EXPIRED without M1 trigger: {setup.model.value} "
                      f"{setup.direction.value} score {setup.score:.1f}")
            self.journal.record_setup(
                now, self._symbol_name, setup, accepted=False,
                rejection_reason="M1 trigger never fired within validity",
                spread_points=self._spread_points(),
                session=setup.session.value, news_status="CLEAR",
                m1_trigger="NONE")
            self._armed = None
            return
        # invalidated if price already broke the structural stop
        last = m1_candles[-1]
        if (setup.direction == Direction.LONG and last.close <= setup.stop_price) \
                or (setup.direction == Direction.SHORT and last.close >= setup.stop_price):
            api.Print("SETUP INVALIDATED before trigger: price broke the "
                      "structural stop level")
            self.journal.record_setup(
                now, self._symbol_name, setup, accepted=False,
                rejection_reason="invalidated: stop level broken pre-entry",
                spread_points=self._spread_points(),
                session=setup.session.value, news_status="CLEAR")
            self._armed = None
            return

        m1_tfa = self.engine.build_tf_analysis(
            Timeframe.M1, m1_candles[-self.cfg.window_m1:])
        confirmation_level = (setup.structure_event.broken_level
                              if setup.structure_event else None)
        result = self.trigger.check(m1_tfa, setup.direction,
                                    confirmation_level)
        if not result.fired:
            self._debug(f"M1 trigger not yet fired: {result.note}")
            return
        setup.m1_trigger = f"{result.kind}: {result.note}"
        api.Print(f"M1 TRIGGER: {result.kind} — {result.note}")
        self._execute(now, setup)
        self._armed = None

    def _execute(self, now, setup):
        cfg = self.cfg
        # live entry reference at trigger time
        entry = float(api.Symbol.Ask) if setup.direction == Direction.LONG \
            else float(api.Symbol.Bid)
        setup.entry_price = entry
        rr1 = setup.rr_to(setup.tp1)
        spread_points = self._spread_points()
        spread_ok, spread_why = self.spread_filter.check(spread_points)
        equity = float(api.Account.Equity)

        if rr1 < cfg.min_rr:
            self._reject_order(now, setup, 0.0, 0.0,
                               f"RR degraded to {rr1:.2f} at trigger time "
                               f"(< {cfg.min_rr})", spread_points)
            return

        risk_fraction = self.sizer.risk_fraction_for(setup.grade.value,
                                                     setup.score)
        self.spec.spread_points = spread_points
        sizing = self.sizer.size(self.spec, equity, risk_fraction,
                                 entry, setup.stop_price)

        unreal = self._floating_pnl()
        lock = self.guard.lock_reason(equity, unreal,
                                      self.sessions.session_at(now))
        allowed, window_why = self.sessions.entry_window_check(now)
        news_blocked, news_reason = self.news.blackout(now)
        facts = OrderFacts(
            is_demo_account=not bool(api.Account.IsLive),
            symbol_is_gold=self._symbol_is_gold(self._symbol_name),
            market_open=bool(api.Symbol.MarketHours.IsOpened()),
            spread_points=spread_points,
            spread_ok=spread_ok,
            # spec: no OTHER gold position, bot-placed or manual
            open_positions_count=self._symbol_positions_count(),
            has_pending_bot_order=self._has_pending_bot_order(),
            news_blocked=news_blocked,
            news_reason=news_reason,
            lock=lock,
            equity=equity,
            session_allowed=allowed,
            session_reason=window_why,
        )
        pf = self.orders.preflight(now, setup, sizing, facts)
        if cfg.debug_logging or not pf.ok:
            for line in pf.checks:
                api.Print(f"  order-safety {line}")
        can, why = self.limits.can_open(
            equity, len(self._bot_positions()),
            open_risk_money=0.0, new_risk_money=sizing.risk_money,
            unrealised=unreal, session=self.sessions.session_at(now))
        if not pf.ok or not can:
            reason = pf.reason if not pf.ok else why
            self._reject_order(now, setup, risk_fraction,
                               sizing.volume_units, reason, spread_points)
            return

        # ---- send the order (SL/TP attached as pip distances, then refined
        #      to the exact structural prices) --------------------------------
        trade_type = TradeType.Buy if setup.direction == Direction.LONG \
            else TradeType.Sell
        sl_pips = abs(entry - setup.stop_price) / self.spec.pip_size
        tp_pips = abs(setup.tp1 - entry) / self.spec.pip_size
        result = api.ExecuteMarketOrder(trade_type, self._symbol_name,
                                        sizing.volume_units, BOT_LABEL,
                                        sl_pips, tp_pips)
        if not bool(result.IsSuccessful) or result.Position is None:
            err = str(result.Error) if result.Error is not None else "unknown"
            api.Print(f"ORDER FAILED: {err} — entering "
                      f"{cfg.order_fail_cooldown_min}min cooldown, no retry "
                      f"spam")
            self.orders.order_failed(now, err)
            self._reject_order(now, setup, risk_fraction,
                               sizing.volume_units,
                               f"broker rejected order: {err}", spread_points)
            return
        self.orders.order_succeeded()
        pos = result.Position
        fill = float(pos.EntryPrice)

        # refine SL/TP to the exact structural prices (never widening the
        # stop beyond the sized risk: only replace if it tightens or matches)
        sl_price = round(setup.stop_price, self.spec.digits)
        tp_price = round(setup.tp1, self.spec.digits)
        try:
            api.ModifyPosition(pos, sl_price, tp_price)
        except Exception as exc:
            api.Print(f"note: could not refine SL/TP to exact prices "
                      f"({exc!r}); pip-based protection from the fill "
                      f"remains active")

        trade = Trade(
            trade_id=new_id("trade"), setup=setup, status=TradeStatus.OPEN,
            volume_units=sizing.volume_units,
            initial_volume_units=sizing.volume_units,
            risk_fraction=sizing.risk_fraction_actual,
            risk_money=sizing.risk_money,
            entry_price=fill, entry_time=now,
            stop_price=sl_price, initial_stop=sl_price,
            tp1=tp_price, tp2=round(setup.tp2, self.spec.digits),
            position_id=int(pos.Id),
        )
        self._tracked = trade
        self.guard.register_open(self.sessions.session_at(now))
        api.Print(f"ORDER FILLED: {setup.direction.value} "
                  f"{sizing.volume_units} units @ {fill:.2f} | SL {sl_price:.2f} "
                  f"TP {tp_price:.2f} | risk {sizing.risk_money:.2f} "
                  f"({sizing.risk_fraction_actual:.2%}) | position "
                  f"{trade.position_id}")
        self.journal.record_setup(
            now, self._symbol_name, setup, accepted=True,
            spread_points=spread_points, session=setup.session.value,
            news_status="CLEAR", risk_pct=sizing.risk_fraction_actual,
            volume_units=sizing.volume_units, m1_trigger=setup.m1_trigger)

    def _reject_order(self, now, setup, risk_fraction, volume, reason,
                      spread_points):
        api.Print(f"SETUP REJECTED at order stage: {reason}")
        self.journal.record_setup(
            now, self._symbol_name, setup, accepted=False,
            rejection_reason=reason, spread_points=spread_points,
            session=setup.session.value, news_status="CLEAR",
            risk_pct=risk_fraction, volume_units=volume,
            m1_trigger=setup.m1_trigger)

    # ------------------------------------------------------- position upkeep
    def _manage_position(self, now):
        trade = self._tracked
        pos = self._find_position(trade.position_id)
        if pos is None:
            return                        # reconcile will handle the close
        trade.bars_open += 1
        m5 = self._completed_candles(Timeframe.M5)
        if len(m5) < self.cfg.atr_period + 10:
            return
        mgmt = self.engine.build_tf_analysis(Timeframe.M5, m5)
        weekend = self.sessions.near_weekend_flat(now)
        for act in self.pos_manager.manage(trade, mgmt, now, self.spec,
                                           weekend_flat=weekend):
            if act.kind == "MOVE_STOP":
                new_stop = round(act.price, self.spec.digits)
                try:
                    tp = pos.TakeProfit
                    api.ModifyPosition(pos, new_stop, tp)
                    trade.stop_price = new_stop
                    api.Print(f"stop moved to {new_stop:.2f} ({act.note}) — "
                              f"stops only ever tighten")
                except Exception as exc:
                    api.Print(f"stop move failed: {exc!r}")
            elif act.kind == "PARTIAL_CLOSE":
                try:
                    r = api.ClosePosition(pos, act.volume_units)
                    if bool(r.IsSuccessful):
                        trade.volume_units = self.spec.round_volume_down(
                            trade.volume_units - act.volume_units)
                        api.Print(f"partial close {act.volume_units} units "
                                  f"({act.note})")
                    else:
                        api.Print(f"partial close failed: {r.Error}")
                except Exception as exc:
                    api.Print(f"partial close failed: {exc!r}")
            elif act.kind == "CLOSE":
                try:
                    r = api.ClosePosition(pos)
                    if bool(r.IsSuccessful):
                        api.Print(f"position closed: {act.note} "
                                  f"({act.reason.value})")
                        trade.exit_reason = act.reason
                    else:
                        api.Print(f"close failed: {r.Error}")
                except Exception as exc:
                    api.Print(f"close failed: {exc!r}")

    def _reconcile_closed(self, now):
        if self._tracked is None:
            return
        trade = self._tracked
        if self._find_position(trade.position_id) is not None:
            return
        # position no longer open: pull the outcome from broker history
        h = self._find_history(trade.position_id)
        if h is not None:
            trade.exit_price = float(h.ClosingPrice)
            trade.exit_time = self._to_utc(h.ClosingTime)
            trade.profit = float(h.NetProfit)
        else:
            trade.exit_time = now
        trade.status = TradeStatus.CLOSED
        if trade.exit_reason is None:
            trade.exit_reason = self._infer_exit_reason(trade)
        self.guard.register_close(trade.profit)
        self.journal.record_trade(trade)
        api.Print(f"TRADE CLOSED: {trade.direction.value} P/L "
                  f"{trade.profit:+.2f} ({trade.r_multiple():+.2f}R) "
                  f"exit {trade.exit_reason.value} | MFE {trade.mfe:+.2f}R "
                  f"MAE {trade.mae:+.2f}R | "
                  f"{self.guard.describe(float(api.Account.Equity))}")
        self._tracked = None

    def _infer_exit_reason(self, trade):
        if trade.exit_price <= 0:
            return ExitReason.BROKER_CLOSED
        tol = 3.0 * self.spec.tick_size + self.spec.spread_points * self.spec.point
        if abs(trade.exit_price - trade.stop_price) <= tol:
            return ExitReason.STOP_LOSS
        if abs(trade.exit_price - trade.tp1) <= tol:
            return ExitReason.TAKE_PROFIT
        return ExitReason.BROKER_CLOSED

    def _update_excursions(self, candle):
        trade = self._tracked
        if trade is None:
            return
        risk = abs(trade.entry_price - trade.initial_stop)
        if risk <= 0:
            return
        d = trade.direction.sign
        fav = (candle.high - trade.entry_price) * d / risk if d > 0 \
            else (trade.entry_price - candle.low) / risk
        adv = (trade.entry_price - candle.low) * d / risk if d > 0 \
            else -(trade.entry_price - candle.high) / risk
        trade.mfe = max(trade.mfe, fav)
        trade.mae = max(trade.mae, adv)

    # ---------------------------------------------------------- state restore
    def _restore_today_from_history(self, now):
        """Replay today's closed bot trades into the daily guard so a
        restart cannot bypass the daily loss lock or trade counters."""
        today = now.date()
        replayed = 0
        for i in range(api.History.Count):
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
            self.guard.register_close(float(h.NetProfit))
            if self.guard.day:
                self.guard.day.trades_opened += 1
            replayed += 1
        if replayed:
            api.Print(f"restart recovery: replayed {replayed} of today's "
                      f"closed bot trades into the daily guard")

    def _adopt_open_position(self, now):
        for pos in self._bot_positions():
            api.Print(f"restart recovery: adopting open bot position "
                      f"{int(pos.Id)} ({str(pos.TradeType)}, "
                      f"{float(pos.VolumeInUnits)} units @ "
                      f"{float(pos.EntryPrice):.2f})")
            sl = float(pos.StopLoss) if pos.StopLoss is not None else 0.0
            tp = float(pos.TakeProfit) if pos.TakeProfit is not None else 0.0
            if sl <= 0:
                api.Print("adopted position has NO stop loss — closing it "
                          "for safety (stop loss is mandatory)")
                try:
                    api.ClosePosition(pos)
                except Exception as exc:
                    api.Print(f"protective close failed: {exc!r}")
                continue
            direction = Direction.LONG if str(pos.TradeType) == "Buy" \
                else Direction.SHORT
            # minimal synthetic setup so management/journal can work
            setup = Setup(
                setup_id=new_id("adopted"), model=SetupModel.TREND_CONTINUATION,
                direction=direction, created_time=now,
                signal_price=float(pos.EntryPrice),
                entry_price=float(pos.EntryPrice), stop_price=sl,
                tp1=tp or float(pos.EntryPrice), tp2=tp or float(pos.EntryPrice),
                runner_target=None, score=0.0, grade=SetupGrade.NO_TRADE,
                breakdown=ScoreBreakdown(), tf_plan=FIXED_PLAN,
                regime=Regime.UNSAFE, session=SessionName.OFF_HOURS,
                htf_bias=TrendState.UNDEFINED,
                reason="adopted after restart")
            risk = abs(float(pos.EntryPrice) - sl) * float(pos.VolumeInUnits) \
                * self.spec.money_per_price_unit_per_unit()
            self._tracked = Trade(
                trade_id=new_id("trade"), setup=setup,
                status=TradeStatus.OPEN,
                volume_units=float(pos.VolumeInUnits),
                initial_volume_units=float(pos.VolumeInUnits),
                risk_fraction=0.0, risk_money=max(risk, 1e-9),
                entry_price=float(pos.EntryPrice),
                entry_time=self._to_utc(pos.EntryTime),
                stop_price=sl, initial_stop=sl, tp1=tp, tp2=tp,
                position_id=int(pos.Id))
            # a position opened today counts toward the daily trade cap
            entry_utc = self._to_utc(pos.EntryTime)
            if self.guard.day and entry_utc.date() == now.date():
                self.guard.register_open(self.sessions.session_at(entry_utc))
            break

    # ------------------------------------------------------------- utilities
    def _apply_ui_parameter_overrides(self):
        """Optional cTrader UI parameters (declared in the companion .cs
        file — see README) override config.py when present.  Only bounded,
        safety-preserving knobs are exposed; there is deliberately no
        live-trading or risk-raising override."""
        cfg = self.cfg

        def take(name, cast, clamp=None):
            try:
                v = getattr(api, name)
            except Exception:
                return None
            try:
                v = cast(v)
            except (TypeError, ValueError):
                return None
            if clamp:
                v = max(clamp[0], min(clamp[1], v))
            return v

        v = take("MinSetupScore", float, (70.0, 100.0))
        if v is not None:
            cfg.min_score = v
        v = take("RiskPercentPerTrade", float, (0.01, 0.25))
        if v is not None:
            cfg.max_risk_per_trade = v / 100.0
        v = take("MaxSpreadPoints", float, (5.0, 200.0))
        if v is not None:
            cfg.max_spread_points = v
        v = take("MaxTradesPerDay", int, (1, 3))
        if v is not None:
            cfg.max_trades_per_day = v
        v = take("MinRewardRisk", float, (1.5, 5.0))
        if v is not None:
            cfg.min_rr = v
        v = take("StopBufferAtr", float, (0.0, 1.0))
        if v is not None:
            cfg.stop_buffer_atr = v
        for pname, attr in (("AsiaEnabled", "asia_enabled"),
                            ("LondonEnabled", "london_enabled"),
                            ("NewYorkEnabled", "newyork_enabled"),
                            ("NewsProtectionEnabled", "news_enabled"),
                            ("BreakEvenEnabled", "breakeven_enabled"),
                            ("TrailingEnabled", "trailing_enabled"),
                            ("PartialTpEnabled", "partial_tp_enabled"),
                            ("EmergencyStop", "emergency_stop"),
                            ("DebugLogging", "debug_logging"),
                            ("StrictMode", "strict_mode")):
            v = take(pname, bool)
            if v is not None:
                setattr(cfg, attr, v)
        v = take("NewsMinutesBefore", int, (0, 240))
        if v is not None:
            cfg.news_block_before_min = v
        v = take("NewsMinutesAfter", int, (0, 240))
        if v is not None:
            cfg.news_block_after_min = v
        v = take("BreakEvenTriggerR", float, (0.5, 5.0))
        if v is not None:
            cfg.breakeven_r = v
        v = take("TrailingAtrMultiple", float, (0.5, 5.0))
        if v is not None:
            cfg.trail_atr_mult = v

    def _symbol_is_gold(self, name):
        norm = "".join(ch for ch in name.upper() if ch.isalnum())
        allowed = {"".join(ch for ch in a.upper() if ch.isalnum())
                   for a in self.cfg.allowed_gold_symbols}
        return norm in allowed

    def _build_spec(self):
        sym = api.Symbol
        return CTraderSymbolSpec(
            name=self._symbol_name,
            digits=int(sym.Digits),
            tick_size=float(sym.TickSize),
            tick_value=float(sym.TickValue),
            pip_size=float(sym.PipSize),
            pip_value=float(sym.PipValue),
            volume_min=float(sym.VolumeInUnitsMin),
            volume_max=float(sym.VolumeInUnitsMax),
            volume_step=float(sym.VolumeInUnitsStep),
            spread_points=self._spread_points_raw(sym),
        )

    def _spread_points_raw(self, sym):
        tick = float(sym.TickSize)
        if tick <= 0:
            return 0.0
        return (float(sym.Ask) - float(sym.Bid)) / tick

    def _spread_points(self):
        return self._spread_points_raw(api.Symbol)

    def _to_utc(self, net_dt):
        """System.DateTime (server time) -> python datetime in UTC.
        cTrader server time is UTC for most brokers; a non-zero
        cfg.server_utc_offset_hours corrects platforms that differ."""
        t = datetime(int(net_dt.Year), int(net_dt.Month), int(net_dt.Day),
                     int(net_dt.Hour), int(net_dt.Minute),
                     int(net_dt.Second), tzinfo=UTC)
        return t - timedelta(hours=self.cfg.server_utc_offset_hours)

    def _now_utc(self):
        return self._to_utc(api.Server.Time)

    def _completed_candles(self, tf):
        """Completed candles for a timeframe — the forming bar (the last
        index) is ALWAYS excluded, so no decision can ever use unfinished
        or future data."""
        bars = self._bars[tf]
        count = int(bars.Count)
        if count < 2:
            return []
        end = count - 1                     # exclude the forming bar
        start = max(0, end - self._windows[tf])
        out = []
        for i in range(start, end):
            out.append(Candle(
                time=self._to_utc(bars.OpenTimes[i]),
                open=float(bars.OpenPrices[i]),
                high=float(bars.HighPrices[i]),
                low=float(bars.LowPrices[i]),
                close=float(bars.ClosePrices[i]),
                volume=float(bars.TickVolumes[i]),
            ))
        return out

    def _bot_positions(self):
        out = []
        for i in range(int(api.Positions.Count)):
            p = api.Positions[i]
            if str(p.SymbolName) == self._symbol_name \
                    and str(p.Label) == BOT_LABEL:
                out.append(p)
        return out

    def _symbol_positions_count(self):
        """ALL open positions on this symbol — manual ones included, so the
        one-GOLD-position rule cannot be bypassed by trading alongside."""
        n = 0
        for i in range(int(api.Positions.Count)):
            if str(api.Positions[i].SymbolName) == self._symbol_name:
                n += 1
        return n

    def _find_position(self, position_id):
        for p in self._bot_positions():
            if int(p.Id) == position_id:
                return p
        return None

    def _has_pending_bot_order(self):
        try:
            for i in range(int(api.PendingOrders.Count)):
                o = api.PendingOrders[i]
                if str(o.SymbolName) == self._symbol_name \
                        and str(o.Label) == BOT_LABEL:
                    return True
        except Exception:
            return False                # bot never places pending orders
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
        for p in self._bot_positions():
            total += float(p.NetProfit)
        return total

    def _emergency_file_present(self):
        name = self.cfg.emergency_stop_file
        candidates = (self.journal.directory,
                      os.path.join(os.path.expanduser("~"), "Documents",
                                   "XAUUSD_Adaptive_Bot"),
                      os.getcwd())
        for base in candidates:
            if base and os.path.exists(os.path.join(base, name)):
                return True
        return self.cfg.emergency_stop

    def _debug(self, msg):
        if self.cfg.debug_logging:
            api.Print(f"[debug] {msg}")
