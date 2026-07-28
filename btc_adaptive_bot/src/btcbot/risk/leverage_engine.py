"""DYNAMIC_LEVERAGE_ENGINE — chooses leverage per entry, always bounded.

Leverage on a perpetual does **not** change the risk of a stop-out — that is
set by position size × stop distance, which the sizer bounds separately. What
leverage changes is:

* **margin efficiency** — how much collateral the position locks up, and
* **liquidation distance** — how far price can move against the position
  before forced liquidation.

So the engine's job is: use the *lowest* leverage that keeps margin usage
sensible, and refuse any leverage whose projected liquidation distance is not
comfortably beyond the stop. Every decision — chosen value, every input, every
adjustment, and every rejection — is journaled to the ``leverage_decisions``
table so the choice can be audited after the fact.

Bounds come from :class:`~btcbot.config.schema.LeverageConfig`; the 10x
ceiling is a hard schema constraint on top of the hard clamp here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config.schema import LeverageConfig
from ..utils.logging import get_logger
from ..utils.numeric import clamp, safe_div

log = get_logger(__name__)


@dataclass(slots=True)
class LeverageInputs:
    """Everything the leverage engine is allowed to consider."""

    entry_price: float
    stop_price: float
    direction: str                    # "long" | "short"
    confidence: float = 0.5
    volatility_pct: float | None = None   # e.g. ATR% of price on the entry timeframe
    regime: str = "UNCERTAIN"
    regime_confidence: float = 0.5
    drawdown_pct: float = 0.0
    risk_state: str = "NORMAL"        # NORMAL | REDUCED | DEFENSIVE | PAUSED
    max_exchange_leverage: float = 10.0   # discovered `lever` from the instrument
    # Maintenance-margin rate for the projected tier; discovered, not assumed.
    # None → the engine uses a conservative default for the liq estimate.
    maintenance_margin_rate: float | None = None


@dataclass(slots=True)
class LeverageDecision:
    """The chosen leverage, with a full audit trail."""

    approved: bool
    leverage: float = 1.0
    reason: str = ""
    reasoning: list[str] = field(default_factory=list)
    adjustments: dict[str, float] = field(default_factory=dict)
    estimated_liq_distance_pct: float = 0.0
    stop_distance_pct: float = 0.0
    liq_buffer_ratio: float = 0.0

    def explain(self) -> str:
        return " | ".join(self.reasoning) if self.reasoning else self.reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "leverage": self.leverage,
            "reason": self.reason,
            "reasoning": self.reasoning,
            "adjustments": self.adjustments,
            "estimated_liq_distance_pct": self.estimated_liq_distance_pct,
            "stop_distance_pct": self.stop_distance_pct,
            "liq_buffer_ratio": self.liq_buffer_ratio,
        }


def _rejected(reason: str, **kwargs: Any) -> LeverageDecision:
    return LeverageDecision(
        approved=False, reason=reason, reasoning=[f"REJECTED: {reason}"], **kwargs
    )


# Conservative maintenance-margin default for the liquidation estimate when the
# position-tiers lookup is unavailable. Real tiers are lower for small BTC
# positions, so this errs toward *over*-estimating liquidation risk.
DEFAULT_MMR = 0.01


class LeverageEngine:
    """Chooses a bounded leverage for one entry and verifies liquidation room."""

    def __init__(self, config: LeverageConfig) -> None:
        self.config = config

    def decide(self, inputs: LeverageInputs) -> LeverageDecision:
        """Run the decision pipeline. Never raises on bad input — it rejects."""
        reasoning: list[str] = []
        adjustments: dict[str, float] = {}

        if inputs.risk_state == "PAUSED":
            return _rejected("risk state is PAUSED — no new entries")
        if inputs.entry_price <= 0 or inputs.stop_price <= 0:
            return _rejected(f"invalid prices (entry {inputs.entry_price}, stop {inputs.stop_price})")

        stop_distance_pct = safe_div(
            abs(inputs.entry_price - inputs.stop_price), inputs.entry_price
        )
        if stop_distance_pct <= 0:
            return _rejected("stop distance is zero")

        # --- 1. base leverage -------------------------------------------
        leverage = self.config.base_leverage
        reasoning.append(f"base leverage {leverage:.1f}x")

        # Confidence: high-conviction entries may carry somewhat more, low
        # conviction less. Scaled inside ±50% of base, then clamped anyway.
        confidence = clamp(inputs.confidence, 0.0, 1.0)
        confidence_factor = 0.75 + 0.5 * confidence
        leverage *= confidence_factor
        adjustments["confidence"] = round(confidence_factor, 4)
        reasoning.append(f"confidence {confidence:.2f} → ×{confidence_factor:.2f}")

        # Volatility: wide conditions get less leverage — the liquidation
        # buffer erodes exactly when slippage risk grows.
        if inputs.volatility_pct and inputs.volatility_pct > 0:
            vol_factor = clamp(safe_div(0.01, inputs.volatility_pct, 1.0), 0.4, 1.25)
            leverage *= vol_factor
            adjustments["volatility"] = round(vol_factor, 4)
            reasoning.append(f"volatility {inputs.volatility_pct * 100:.2f}% → ×{vol_factor:.2f}")
            if inputs.volatility_pct > 0.02 and leverage > self.config.high_vol_leverage_cap:
                leverage = self.config.high_vol_leverage_cap
                reasoning.append(
                    f"high-volatility cap applied → {self.config.high_vol_leverage_cap:.1f}x"
                )

        # Regime: an uncertain regime read means less leverage.
        regime_factor = clamp(0.7 + 0.3 * clamp(inputs.regime_confidence, 0.0, 1.0), 0.7, 1.0)
        leverage *= regime_factor
        adjustments["regime"] = round(regime_factor, 4)

        # Drawdown / risk state.
        if inputs.risk_state in {"REDUCED", "DEFENSIVE"} or inputs.drawdown_pct >= 0.08:
            cap = self.config.defensive_leverage_cap
            if leverage > cap:
                leverage = cap
                reasoning.append(
                    f"risk state {inputs.risk_state} / drawdown "
                    f"{inputs.drawdown_pct * 100:.1f}% → capped at {cap:.1f}x"
                )
            adjustments["defensive_cap"] = cap

        # --- 2. HARD BOUNDS ---------------------------------------------
        exchange_max = max(1.0, inputs.max_exchange_leverage)
        bounded = clamp(
            leverage,
            self.config.min_leverage,
            min(self.config.max_leverage, exchange_max),
        )
        if abs(bounded - leverage) > 1e-9:
            reasoning.append(
                f"clamped {leverage:.2f}x → {bounded:.2f}x "
                f"(bounds {self.config.min_leverage:.0f}x–"
                f"{min(self.config.max_leverage, exchange_max):.0f}x)"
            )
        leverage = round(bounded, 1)

        # OKX accepts integer-ish leverage strings; snap to a clean step.
        leverage = max(self.config.min_leverage, round(leverage * 2) / 2)

        # --- 3. LIQUIDATION PROTECTION ----------------------------------
        # Approximate liquidation distance for isolated margin:
        #   liq_distance ≈ 1/leverage − mmr  (as a fraction of entry price)
        # This ignores fee reserves, which only makes the estimate more
        # conservative when mmr comes from the discovered position tier.
        mmr = inputs.maintenance_margin_rate if inputs.maintenance_margin_rate else DEFAULT_MMR
        liq_distance_pct = max(0.0, (1.0 / leverage) - mmr)
        buffer_ratio = safe_div(liq_distance_pct, stop_distance_pct)

        while (
            buffer_ratio < self.config.liq_buffer_stop_ratio
            and leverage > self.config.min_leverage
        ):
            # Step leverage down until the stop sits comfortably inside the
            # liquidation distance.
            leverage = max(self.config.min_leverage, leverage - 0.5)
            liq_distance_pct = max(0.0, (1.0 / leverage) - mmr)
            buffer_ratio = safe_div(liq_distance_pct, stop_distance_pct)

        if buffer_ratio < self.config.liq_buffer_stop_ratio:
            return _rejected(
                f"even at {leverage:.1f}x the projected liquidation distance "
                f"({liq_distance_pct * 100:.2f}%) is under "
                f"{self.config.liq_buffer_stop_ratio:.1f}× the stop distance "
                f"({stop_distance_pct * 100:.2f}%) — entry refused",
                estimated_liq_distance_pct=liq_distance_pct,
                stop_distance_pct=stop_distance_pct,
                liq_buffer_ratio=buffer_ratio,
            )

        reasoning.append(
            f"final {leverage:.1f}x — projected liq distance {liq_distance_pct * 100:.2f}% "
            f"= {buffer_ratio:.1f}× stop distance "
            f"(required ≥ {self.config.liq_buffer_stop_ratio:.1f}×, mmr {mmr * 100:.2f}%)"
        )

        return LeverageDecision(
            approved=True,
            leverage=leverage,
            reason="approved",
            reasoning=reasoning,
            adjustments=adjustments,
            estimated_liq_distance_pct=liq_distance_pct,
            stop_distance_pct=stop_distance_pct,
            liq_buffer_ratio=buffer_ratio,
        )
