"""
Data model: enums and dataclasses shared by every layer.

Ported from the legacy single-file bot with the broker-specific pieces
replaced: MT5 tickets became cTrader position ids, and the MT5 symbol
specification became CTraderSymbolSpec, which is populated in the main cBot
file from the live cTrader Symbol object (tick size, tick value, pip size,
volume min/max/step in UNITS — never assumed, always read from the broker).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple


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
