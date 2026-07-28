"""The ten-layer decision engine.

Every candidate signal that might become a real demo order passes through ten
explicit layers, in order. Each layer either passes the signal on or refuses
it — and every refusal is journaled to ``rejected_signals`` with the layer's
index, name, reason, and context, so "why didn't it trade?" is always
answerable from the database.

The ten layers:

==  =======================  =============================================
 #  Layer                    Where it runs
==  =======================  =============================================
 1  data_health              here — market data fresh, order book valid
 2  signal_integrity         here — numerically sane output, sane R/R
 3  regime_alignment         here — regime is one the strategy allows
 4  htf_alignment            here — not fighting a confident higher-TF trend
 5  confirmation             here — order-flow does not strongly oppose
 6  news_risk                here — no high-impact event window
 7  risk_state               here — NORMAL/REDUCED/DEFENSIVE/PAUSED gating
 8  leverage_engine          executor — DYNAMIC_LEVERAGE_ENGINE + liq buffer
 9  position_sizing          executor — contracts, margin, hard risk caps
10  order_safety             executor — duplicates, breakers, set-and-confirm
==  =======================  =============================================

Layers 8–10 live in :class:`~btcbot.execution.demo_executor.DemoExecutor`
because they need exchange state; they journal to the same table with the
same layer numbering, so the journal reads as one continuous pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..database.repositories import RejectedSignalRepository
from ..regime.classifier import Regime
from ..strategies.base import Direction, Strategy, StrategyCategory, StrategyContext, StrategySignal
from ..utils.logging import get_logger
from ..utils.timeutil import iso, now_utc
from .risk_state import DEFENSIVE, PAUSED, RiskStateSnapshot

log = get_logger(__name__)

# Regimes that represent a confident higher-timeframe directional trend.
_BULL_REGIMES = {Regime.STRONG_TREND_UP}
_BEAR_REGIMES = {Regime.STRONG_TREND_DOWN}
# Strategy families whose entire hypothesis is trading *against* the prevailing
# move — the counter-trend layer must not veto their reason to exist.
_COUNTER_TREND_EXEMPT = {StrategyCategory.MEAN_REVERSION, StrategyCategory.STRUCTURE}

# News risk levels, mapped from the news engine's continuous risk score.
NEWS_NORMAL = "NORMAL"
NEWS_ELEVATED = "ELEVATED"
NEWS_HIGH = "HIGH"
NEWS_EXTREME = "EXTREME"


def news_risk_label(risk: float, *, blocks_entry: bool) -> str:
    if blocks_entry or risk >= 0.85:
        return NEWS_EXTREME
    if risk >= 0.6:
        return NEWS_HIGH
    if risk >= 0.3:
        return NEWS_ELEVATED
    return NEWS_NORMAL


@dataclass(slots=True)
class LayerResult:
    index: int
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(slots=True)
class DecisionOutcome:
    """The pipeline verdict for one candidate signal."""

    accepted: bool
    layers: list[LayerResult] = field(default_factory=list)
    news_state: str = NEWS_NORMAL
    risk_state: str = "NORMAL"

    @property
    def rejection(self) -> LayerResult | None:
        return next((layer for layer in self.layers if not layer.passed), None)

    def describe(self) -> str:
        if self.accepted:
            return f"accepted through {len(self.layers)} layers"
        rejection = self.rejection
        return f"rejected at layer {rejection.index} ({rejection.name}): {rejection.detail}" if rejection else "rejected"


class DecisionEngine:
    """Runs layers 1–7 for every demo-order candidate."""

    def __init__(
        self,
        rejected_signals: RejectedSignalRepository,
        *,
        experiment_id: str,
    ) -> None:
        self.rejected_signals = rejected_signals
        self.experiment_id = experiment_id
        self.evaluated = 0
        self.accepted = 0

    def evaluate(
        self,
        strategy: Strategy,
        signal: StrategySignal,
        context: StrategyContext,
        *,
        data_healthy: bool,
        data_detail: str,
        risk_state: RiskStateSnapshot,
        signal_id: str | None = None,
        setup_id: str | None = None,
    ) -> DecisionOutcome:
        """Run layers 1–7. Journals the first failing layer, if any."""
        self.evaluated += 1
        layers: list[LayerResult] = []
        news_label = news_risk_label(context.news_risk, blocks_entry=context.news_blocks_entry)

        def refuse(index: int, name: str, detail: str) -> DecisionOutcome:
            layers.append(LayerResult(index, name, False, detail))
            self._journal(
                signal,
                signal_id=signal_id,
                setup_id=setup_id,
                layer_index=index,
                layer_name=name,
                reason=detail,
                detail={
                    "news_state": news_label,
                    "risk_state": risk_state.state,
                    "layers_passed": [layer.name for layer in layers if layer.passed],
                },
            )
            return DecisionOutcome(
                accepted=False, layers=layers, news_state=news_label, risk_state=risk_state.state
            )

        def ok(index: int, name: str, detail: str) -> None:
            layers.append(LayerResult(index, name, True, detail))

        # --- layer 1: data health ---------------------------------------
        if not data_healthy:
            return refuse(1, "data_health", f"market data unhealthy: {data_detail}")
        if not context.features.orderbook_valid:
            ok(1, "data_health", "candles fresh; order book still syncing (flow checks soften)")
        else:
            ok(1, "data_health", "all streams fresh")

        # --- layer 2: signal integrity ----------------------------------
        stop_pct = signal.stop_distance_pct
        if stop_pct <= 0:
            return refuse(2, "signal_integrity", "stop distance is not positive")
        rr = signal.rr_ratio
        if rr is not None and rr < 0.5:
            return refuse(
                2, "signal_integrity", f"reward:risk {rr:.2f} below 0.5 — structurally poor trade"
            )
        ok(2, "signal_integrity", f"stop {stop_pct * 100:.2f}%, R/R {f'{rr:.2f}' if rr else 'n/a'}")

        # --- layer 3: regime alignment ----------------------------------
        allowed = strategy.allowed_regimes()
        if allowed and signal.regime not in allowed:
            return refuse(
                3,
                "regime_alignment",
                f"regime {signal.regime.value} is outside the strategy's allowed set",
            )
        ok(3, "regime_alignment", f"{signal.regime.value} permitted for {strategy.id}")

        # --- layer 4: higher-timeframe alignment ------------------------
        regime = context.regime
        if regime.confidence >= 0.7 and strategy.category not in _COUNTER_TREND_EXEMPT:
            if signal.direction is Direction.SHORT and regime.regime in _BULL_REGIMES:
                return refuse(
                    4,
                    "htf_alignment",
                    f"short against {regime.regime.value} "
                    f"(confidence {regime.confidence:.2f}) — counter-trend veto",
                )
            if signal.direction is Direction.LONG and regime.regime in _BEAR_REGIMES:
                return refuse(
                    4,
                    "htf_alignment",
                    f"long against {regime.regime.value} "
                    f"(confidence {regime.confidence:.2f}) — counter-trend veto",
                )
        ok(4, "htf_alignment", "not fighting a confident higher-timeframe trend")

        # --- layer 5: order-flow confirmation ---------------------------
        if context.features.orderbook_valid:
            imbalance = context.features.orderbook_imbalance
            if signal.direction is Direction.LONG and imbalance < -0.6:
                return refuse(
                    5,
                    "confirmation",
                    f"order book strongly ask-heavy ({imbalance:+.2f}) against a long entry",
                )
            if signal.direction is Direction.SHORT and imbalance > 0.6:
                return refuse(
                    5,
                    "confirmation",
                    f"order book strongly bid-heavy ({imbalance:+.2f}) against a short entry",
                )
            ok(5, "confirmation", f"order-flow imbalance {imbalance:+.2f} acceptable")
        else:
            ok(5, "confirmation", "order book not yet valid — flow check skipped, size layers compensate")

        # --- layer 6: news risk -----------------------------------------
        if context.news_blocks_entry:
            return refuse(6, "news_risk", f"news state {news_label}: high-impact event window")
        ok(6, "news_risk", f"news state {news_label} (risk {context.news_risk:.2f})")

        # --- layer 7: risk state ----------------------------------------
        if risk_state.state == PAUSED:
            return refuse(7, "risk_state", f"PAUSED: {risk_state.reason}")
        if risk_state.state == DEFENSIVE and signal.confidence < 0.7:
            return refuse(
                7,
                "risk_state",
                f"DEFENSIVE state accepts only high-conviction entries "
                f"(confidence {signal.confidence:.2f} < 0.70)",
            )
        ok(7, "risk_state", f"{risk_state.state}: {risk_state.reason}")

        self.accepted += 1
        return DecisionOutcome(
            accepted=True, layers=layers, news_state=news_label, risk_state=risk_state.state
        )

    def _journal(
        self,
        signal: StrategySignal,
        *,
        signal_id: str | None,
        setup_id: str | None,
        layer_index: int,
        layer_name: str,
        reason: str,
        detail: dict[str, Any],
    ) -> None:
        try:
            self.rejected_signals.record(
                {
                    "experiment_id": self.experiment_id,
                    "ts_utc": iso(now_utc()),
                    "signal_id": signal_id,
                    "setup_id": setup_id,
                    "strategy_id": signal.strategy_id,
                    "strategy_version": signal.strategy_version,
                    "inst_id": signal.symbol,
                    "direction": signal.direction.value,
                    "layer_index": layer_index,
                    "layer_name": layer_name,
                    "reason": reason,
                    "detail": detail,
                    "regime": signal.regime.value,
                    "confidence": signal.confidence,
                }
            )
        except Exception as exc:  # noqa: BLE001 - journaling must not break the pipeline
            log.warning("DECISION", f"Could not journal layer rejection: {exc}")

    def stats(self) -> dict[str, Any]:
        return {"evaluated": self.evaluated, "accepted": self.accepted}
