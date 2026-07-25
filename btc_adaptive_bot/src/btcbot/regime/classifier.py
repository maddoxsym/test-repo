"""Market regime classification.

The brief is explicit: *do not classify a regime from one arbitrary indicator
alone*. This engine therefore collects independent measurements — directional
strength, trend persistence, slope, structure, volatility level, volatility
change, range compression, volume, VWAP deviation — and each casts a weighted
vote. The winning regime is the one with the most evidence, and the margin of
victory becomes the confidence score.

Low confidence resolves to ``UNCERTAIN`` rather than a coin-flip label, because
downstream code uses the regime to gate strategies and a wrong-but-confident
label is worse than an honest "don't know".
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from ..config.schema import RegimeConfig
from ..features.engine import FeatureSet
from ..utils.numeric import clamp, safe_div


class Regime(str, Enum):
    STRONG_TREND_UP = "STRONG_TREND_UP"
    TREND_UP = "TREND_UP"
    STRONG_TREND_DOWN = "STRONG_TREND_DOWN"
    TREND_DOWN = "TREND_DOWN"
    RANGING = "RANGING"
    BREAKOUT = "BREAKOUT"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    VOLATILITY_CONTRACTION = "VOLATILITY_CONTRACTION"
    UNCERTAIN = "UNCERTAIN"


TRENDING_REGIMES = frozenset(
    {Regime.STRONG_TREND_UP, Regime.TREND_UP, Regime.STRONG_TREND_DOWN, Regime.TREND_DOWN}
)
BULLISH_REGIMES = frozenset({Regime.STRONG_TREND_UP, Regime.TREND_UP})
BEARISH_REGIMES = frozenset({Regime.STRONG_TREND_DOWN, Regime.TREND_DOWN})
QUIET_REGIMES = frozenset({Regime.RANGING, Regime.LOW_VOLATILITY, Regime.VOLATILITY_CONTRACTION})
ALL_REGIMES = frozenset(Regime)

# Regime families for confidence scoring. Labels inside a family corroborate one
# another; labels in *conflicting* families are mutually exclusive readings of
# the same market.
_FAMILY_BULLISH = "bullish_trend"
_FAMILY_BEARISH = "bearish_trend"
_FAMILY_QUIET = "quiet"
_FAMILY_ACTIVE = "active"
_FAMILY_UNKNOWN = "unknown"

_REGIME_FAMILIES: dict[Regime, str] = {
    Regime.STRONG_TREND_UP: _FAMILY_BULLISH,
    Regime.TREND_UP: _FAMILY_BULLISH,
    Regime.STRONG_TREND_DOWN: _FAMILY_BEARISH,
    Regime.TREND_DOWN: _FAMILY_BEARISH,
    Regime.RANGING: _FAMILY_QUIET,
    Regime.LOW_VOLATILITY: _FAMILY_QUIET,
    Regime.VOLATILITY_CONTRACTION: _FAMILY_QUIET,
    Regime.BREAKOUT: _FAMILY_ACTIVE,
    Regime.HIGH_VOLATILITY: _FAMILY_ACTIVE,
    Regime.VOLATILITY_EXPANSION: _FAMILY_ACTIVE,
    Regime.UNCERTAIN: _FAMILY_UNKNOWN,
}

# Pairs that genuinely contradict each other. A bullish trend that is also
# breaking out is coherent; a bullish trend that is also bearish is not, and a
# quiet range that is also expanding is not.
_CONFLICTS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({_FAMILY_BULLISH, _FAMILY_BEARISH}),
        frozenset({_FAMILY_BULLISH, _FAMILY_QUIET}),
        frozenset({_FAMILY_BEARISH, _FAMILY_QUIET}),
        frozenset({_FAMILY_QUIET, _FAMILY_ACTIVE}),
    }
)


def _family_of(regime: Regime) -> str:
    return _REGIME_FAMILIES.get(regime, _FAMILY_UNKNOWN)


def _families_conflict(first: str, second: str) -> bool:
    if first == second:
        return False
    return frozenset({first, second}) in _CONFLICTS


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    """A classification plus the measurements that produced it."""

    regime: Regime
    confidence: float
    bar_open_ms: int
    symbol: str
    timeframe: str
    adx: float = float("nan")
    atr: float = float("nan")
    atr_pct: float = float("nan")
    realized_vol: float = float("nan")
    ma_slope: float = float("nan")
    trend_persistence: float = float("nan")
    range_percentile: float = float("nan")
    volume_z: float = float("nan")
    vwap_deviation: float = float("nan")
    vol_ratio: float = float("nan")
    evidence: dict[str, float] = field(default_factory=dict)
    context_regime: Regime | None = None

    @property
    def is_trending(self) -> bool:
        return self.regime in TRENDING_REGIMES

    @property
    def direction_bias(self) -> int:
        """+1 bullish, -1 bearish, 0 neutral."""
        if self.regime in BULLISH_REGIMES:
            return 1
        if self.regime in BEARISH_REGIMES:
            return -1
        return 0

    def describe(self) -> str:
        return f"{self.regime.value} (confidence {self.confidence:.2f})"

    def to_row(self, *, experiment_id: str | None, ts_utc: str) -> dict[str, Any]:
        return {
            "experiment_id": experiment_id,
            "ts_utc": ts_utc,
            "bar_open_ms": self.bar_open_ms,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "regime": self.regime.value,
            "confidence": self.confidence,
            "adx": _finite(self.adx),
            "atr": _finite(self.atr),
            "atr_pct": _finite(self.atr_pct),
            "realized_vol": _finite(self.realized_vol),
            "ma_slope": _finite(self.ma_slope),
            "trend_persistence": _finite(self.trend_persistence),
            "range_percentile": _finite(self.range_percentile),
            "volume_z": _finite(self.volume_z),
            "vwap_deviation": _finite(self.vwap_deviation),
            "evidence": self.evidence,
        }


def _finite(value: float) -> float | None:
    return float(value) if np.isfinite(value) else None


class RegimeClassifier:
    """Weighted multi-feature regime voter."""

    # Relative influence of each evidence source. Directional strength and
    # persistence dominate because they are the most robust trend measures;
    # volume alone never decides a regime.
    WEIGHTS = {
        "directional_strength": 1.0,
        "trend_persistence": 0.9,
        "slope": 0.8,
        "structure": 0.7,
        "volatility_level": 0.8,
        "volatility_change": 0.9,
        "range_state": 0.7,
        "breakout": 0.9,
        "volume": 0.35,
        "vwap": 0.3,
    }

    def __init__(self, config: RegimeConfig) -> None:
        self.config = config

    def classify(
        self, features: FeatureSet, *, context: FeatureSet | None = None
    ) -> RegimeSnapshot:
        """Classify the regime as of ``features``' last closed bar."""
        votes: dict[Regime, float] = defaultdict(float)
        evidence: dict[str, float] = {}

        adx = features.last("adx14")
        plus_di = features.last("plus_di")
        minus_di = features.last("minus_di")
        slope = features.last("ma_slope")
        atr_value = features.last("atr14")
        atr_pct = features.last("atr_pct")
        realized_vol = features.last("realized_vol")
        realized_vol_long = features.last("realized_vol_long")
        range_pct = features.last("range_percentile")
        bandwidth_pct = features.last("bb_bandwidth_pct")
        volume_z = features.last("volume_z")
        vwap_dev = features.last("vwap_deviation")

        # --- 1. directional strength (ADX + DI spread) --------------------
        if np.isfinite(adx) and np.isfinite(plus_di) and np.isfinite(minus_di):
            weight = self.WEIGHTS["directional_strength"]
            bullish = plus_di > minus_di
            if adx >= self.config.adx_strong_trend_threshold:
                votes[Regime.STRONG_TREND_UP if bullish else Regime.STRONG_TREND_DOWN] += weight
                votes[Regime.TREND_UP if bullish else Regime.TREND_DOWN] += weight * 0.5
            elif adx >= self.config.adx_trend_threshold:
                votes[Regime.TREND_UP if bullish else Regime.TREND_DOWN] += weight
            else:
                votes[Regime.RANGING] += weight
            evidence["adx"] = round(float(adx), 2)

        # --- 2. trend persistence (fraction of bars above/below EMA) ------
        persistence = self._trend_persistence(features)
        if np.isfinite(persistence):
            weight = self.WEIGHTS["trend_persistence"]
            if persistence > 0.7:
                votes[Regime.TREND_UP] += weight
                votes[Regime.STRONG_TREND_UP] += weight * 0.4
            elif persistence < 0.3:
                votes[Regime.TREND_DOWN] += weight
                votes[Regime.STRONG_TREND_DOWN] += weight * 0.4
            else:
                votes[Regime.RANGING] += weight * 0.8
            evidence["trend_persistence"] = round(float(persistence), 3)

        # --- 3. moving-average slope --------------------------------------
        if np.isfinite(slope):
            weight = self.WEIGHTS["slope"]
            # Slope is normalised by price; 0.0006/bar is a meaningful drift.
            if slope > 0.0006:
                votes[Regime.TREND_UP] += weight
                if slope > 0.0018:
                    votes[Regime.STRONG_TREND_UP] += weight * 0.7
            elif slope < -0.0006:
                votes[Regime.TREND_DOWN] += weight
                if slope < -0.0018:
                    votes[Regime.STRONG_TREND_DOWN] += weight * 0.7
            else:
                votes[Regime.RANGING] += weight * 0.7
            evidence["ma_slope"] = round(float(slope), 6)

        # --- 4. structure (higher highs / lower lows) ---------------------
        structure = self._structure_score(features)
        if structure is not None:
            weight = self.WEIGHTS["structure"]
            if structure > 0:
                votes[Regime.TREND_UP] += weight * structure
            elif structure < 0:
                votes[Regime.TREND_DOWN] += weight * abs(structure)
            else:
                votes[Regime.RANGING] += weight * 0.5
            evidence["structure"] = round(float(structure), 3)

        # --- 5. volatility level ------------------------------------------
        if np.isfinite(realized_vol) and np.isfinite(realized_vol_long) and realized_vol_long > 0:
            weight = self.WEIGHTS["volatility_level"]
            ratio = realized_vol / realized_vol_long
            if ratio >= self.config.vol_expansion_ratio:
                votes[Regime.HIGH_VOLATILITY] += weight
            elif ratio <= self.config.vol_contraction_ratio:
                votes[Regime.LOW_VOLATILITY] += weight
            evidence["vol_ratio"] = round(float(ratio), 3)
        else:
            ratio = float("nan")

        # --- 6. volatility change (expansion / contraction) ---------------
        vol_change = self._volatility_change(features)
        if np.isfinite(vol_change):
            weight = self.WEIGHTS["volatility_change"]
            if vol_change >= self.config.vol_expansion_ratio:
                votes[Regime.VOLATILITY_EXPANSION] += weight
            elif vol_change <= self.config.vol_contraction_ratio:
                votes[Regime.VOLATILITY_CONTRACTION] += weight
            evidence["vol_change"] = round(float(vol_change), 3)

        # --- 7. range compression / expansion -----------------------------
        gauge = bandwidth_pct if np.isfinite(bandwidth_pct) else range_pct
        if np.isfinite(gauge):
            weight = self.WEIGHTS["range_state"]
            if gauge <= self.config.range_compression_percentile:
                votes[Regime.VOLATILITY_CONTRACTION] += weight
                votes[Regime.RANGING] += weight * 0.5
            elif gauge >= self.config.range_expansion_percentile:
                votes[Regime.VOLATILITY_EXPANSION] += weight * 0.7
            evidence["range_percentile"] = round(float(gauge), 1)

        # --- 8. breakout detection ----------------------------------------
        breakout = self._breakout_score(features)
        if breakout > 0:
            votes[Regime.BREAKOUT] += self.WEIGHTS["breakout"] * breakout
            evidence["breakout"] = round(float(breakout), 3)

        # --- 9. volume corroboration --------------------------------------
        if np.isfinite(volume_z):
            weight = self.WEIGHTS["volume"]
            if volume_z > 1.5:
                votes[Regime.BREAKOUT] += weight * 0.6
                votes[Regime.VOLATILITY_EXPANSION] += weight * 0.4
            elif volume_z < -0.8:
                votes[Regime.LOW_VOLATILITY] += weight * 0.5
                votes[Regime.RANGING] += weight * 0.3
            evidence["volume_z"] = round(float(volume_z), 2)

        # --- 10. VWAP deviation -------------------------------------------
        if np.isfinite(vwap_dev):
            weight = self.WEIGHTS["vwap"]
            if abs(vwap_dev) < 0.002:
                votes[Regime.RANGING] += weight
            elif vwap_dev > 0.006:
                votes[Regime.TREND_UP] += weight
            elif vwap_dev < -0.006:
                votes[Regime.TREND_DOWN] += weight
            evidence["vwap_deviation"] = round(float(vwap_dev), 5)

        regime, confidence = self._resolve(votes)

        context_regime: Regime | None = None
        if context is not None:
            context_votes: dict[Regime, float] = defaultdict(float)
            context_adx = context.last("adx14")
            context_plus = context.last("plus_di")
            context_minus = context.last("minus_di")
            if np.isfinite(context_adx) and np.isfinite(context_plus) and np.isfinite(context_minus):
                bullish = context_plus > context_minus
                if context_adx >= self.config.adx_trend_threshold:
                    context_votes[Regime.TREND_UP if bullish else Regime.TREND_DOWN] += 1.0
                else:
                    context_votes[Regime.RANGING] += 1.0
            context_regime = self._resolve(context_votes)[0] if context_votes else None

        return RegimeSnapshot(
            regime=regime,
            confidence=confidence,
            bar_open_ms=features.bar_open_ms,
            symbol=features.symbol,
            timeframe=features.timeframe,
            adx=adx,
            atr=atr_value,
            atr_pct=atr_pct,
            realized_vol=realized_vol,
            ma_slope=slope,
            trend_persistence=persistence,
            range_percentile=gauge,
            volume_z=volume_z,
            vwap_deviation=vwap_dev,
            vol_ratio=ratio,
            evidence=evidence,
            context_regime=context_regime,
        )

    # --- evidence helpers -------------------------------------------------

    def _resolve(self, votes: dict[Regime, float]) -> tuple[Regime, float]:
        """Pick the winner and derive confidence from the evidence's coherence.

        Confidence is measured against *disagreement*, not against the number of
        labels that received votes. Several regimes in the same family
        (``TREND_UP`` and ``STRONG_TREND_UP``, say) are corroborating evidence,
        not competing hypotheses — scoring them as rivals made a textbook strong
        uptrend resolve to ``UNCERTAIN`` purely because its votes were split
        between two labels that agreed with each other.

        So: ``share`` is the winning *family's* portion of all votes, and
        ``margin`` compares the winner with the strongest genuinely conflicting
        family.
        """
        if not votes:
            return (Regime.UNCERTAIN, 0.0)
        total = sum(votes.values())
        if total <= 0:
            return (Regime.UNCERTAIN, 0.0)

        winner, winner_score = max(votes.items(), key=lambda kv: kv[1])
        family = _family_of(winner)

        family_score = sum(score for regime, score in votes.items() if _family_of(regime) == family)
        conflicting = [
            score
            for regime, score in votes.items()
            if _families_conflict(family, _family_of(regime))
        ]
        best_conflict = max(conflicting) if conflicting else 0.0

        share = safe_div(family_score, total)
        margin = safe_div(winner_score - best_conflict, max(winner_score, 1e-9))
        confidence = clamp(0.55 * share + 0.45 * margin, 0.0, 1.0)

        if confidence < self.config.min_confidence:
            return (Regime.UNCERTAIN, confidence)
        return (winner, confidence)

    def _trend_persistence(self, features: FeatureSet) -> float:
        """Fraction of recent closes above EMA50 — 1.0 = relentless uptrend."""
        lookback = self.config.persistence_lookback
        close = features.series("close")
        ema50 = features.series("ema50")
        if close.size < lookback or ema50.size < lookback:
            return float("nan")
        recent_close = close[-lookback:]
        recent_ema = ema50[-lookback:]
        valid = np.isfinite(recent_ema)
        if valid.sum() < lookback // 2:
            return float("nan")
        return float(np.mean(recent_close[valid] > recent_ema[valid]))

    def _volatility_change(self, features: FeatureSet) -> float:
        """Ratio of current ATR to its own level ``lookback`` bars ago."""
        atr_series = features.series("atr14")
        lookback = self.config.slope_lookback
        if atr_series.size < lookback + 1:
            return float("nan")
        current = atr_series[-1]
        past = atr_series[-1 - lookback]
        if not (np.isfinite(current) and np.isfinite(past)) or past <= 0:
            return float("nan")
        return float(current / past)

    def _structure_score(self, features: FeatureSet) -> float | None:
        """+1 higher highs *and* higher lows, -1 the mirror, 0 mixed."""
        highs = features.series("high")
        lows = features.series("low")
        swing_high_mask = features.series("swing_high")
        swing_low_mask = features.series("swing_low")
        if highs.size < 40 or swing_high_mask.size != highs.size:
            return None

        high_idx = np.flatnonzero(swing_high_mask[-120:] > 0)
        low_idx = np.flatnonzero(swing_low_mask[-120:] > 0)
        if high_idx.size < 2 or low_idx.size < 2:
            return None

        window_highs = highs[-120:]
        window_lows = lows[-120:]
        higher_high = window_highs[high_idx[-1]] > window_highs[high_idx[-2]]
        higher_low = window_lows[low_idx[-1]] > window_lows[low_idx[-2]]

        if higher_high and higher_low:
            return 1.0
        if not higher_high and not higher_low:
            return -1.0
        return 0.0

    def _breakout_score(self, features: FeatureSet) -> float:
        """How decisively the last bar cleared its prior Donchian channel."""
        close = features.close
        # Compare against the *previous* bar's channel: using the current bar's
        # channel would include the breakout bar itself and always self-confirm.
        upper = features.prev("donchian_upper")
        lower = features.prev("donchian_lower")
        atr_value = features.last("atr14")
        if not (np.isfinite(upper) and np.isfinite(lower) and np.isfinite(atr_value)) or atr_value <= 0:
            return 0.0
        if close > upper:
            return float(clamp((close - upper) / atr_value, 0.0, 1.0))
        if close < lower:
            return float(clamp((lower - close) / atr_value, 0.0, 1.0))
        return 0.0


class RegimeTracker:
    """Keeps recent regime history for stability analysis and reporting."""

    def __init__(self, *, max_history: int = 2000) -> None:
        self._history: list[RegimeSnapshot] = []
        self._max = max_history

    def add(self, snapshot: RegimeSnapshot) -> bool:
        """Append a snapshot; returns True when the regime label changed."""
        changed = bool(self._history) and self._history[-1].regime is not snapshot.regime
        if self._history and self._history[-1].bar_open_ms == snapshot.bar_open_ms:
            self._history[-1] = snapshot
            return False
        self._history.append(snapshot)
        if len(self._history) > self._max:
            del self._history[: len(self._history) - self._max]
        return changed

    @property
    def current(self) -> RegimeSnapshot | None:
        return self._history[-1] if self._history else None

    def distribution(self, lookback: int = 500) -> dict[str, float]:
        """Share of recent bars spent in each regime."""
        window = self._history[-lookback:]
        if not window:
            return {}
        counts: dict[str, int] = defaultdict(int)
        for snapshot in window:
            counts[snapshot.regime.value] += 1
        return {name: count / len(window) for name, count in counts.items()}

    def stability(self, lookback: int = 100) -> float:
        """Fraction of adjacent bars that kept the same label (1.0 = very stable)."""
        window = self._history[-lookback:]
        if len(window) < 2:
            return 1.0
        unchanged = sum(
            1 for a, b in zip(window, window[1:], strict=False) if a.regime is b.regime
        )
        return unchanged / (len(window) - 1)

    def history(self, limit: int = 100) -> list[RegimeSnapshot]:
        return self._history[-limit:]
