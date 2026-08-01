"""Actual-trade eligibility — can this setup pay for itself?

The problem this solves
-----------------------

The allocator was repeatedly picking setups that could never survive the
executor. Live logs showed selected candidates with stop distances of
0.034%–0.043% against a required minimum of 0.080%, and others where total
round-trip costs came to 190%–310% of the target profit. Every one of them was
logged as ``allocated actual Demo trade`` and then refused by the sizing and
risk gates a moment later.

That is three problems at once. The allocator burned its exploration budget on
candidates guaranteed to fail. The logs claimed an actual trade had been
allocated when none could be. And the "demo orders blocked" counter grew for
signals that never became order candidates in the first place.

The rule
--------

**A setup is only an actual-trade candidate if it can pay for itself.** The
arithmetic is not subtle: at OKX's demo taker rate a round trip costs two full
taker fees plus the spread plus slippage on both legs. A 1-minute
microstructure setup targeting 0.05% is not a marginal trade, it is a
guaranteed loss — the costs are an order of magnitude larger than the edge.

So this filter runs **before** allocation. It measures the setup against the
costs it will actually pay and answers one question: ELIGIBLE, or SHADOW_ONLY.
Ineligible setups keep trading in shadow research, where they cost nothing and
still generate evidence; they simply never reach the allocator.

What this is not
----------------

It is **not** a replacement for the executor's gates. Layers 8–10 (leverage,
sizing, order safety) still run unchanged on everything that gets through, as
an independent second check. This filter exists so the allocator stops
*choosing* candidates that those gates will certainly refuse — a first opinion,
never the last word.

It is also not a timeframe rule. No strategy is banned and none is waved
through; a 1m setup with a genuinely wide target passes, and a 1h setup with a
thin one does not. Timeframe correlates with target size, but the filter
measures the target.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config.schema import ActualEligibilityConfig, RiskConfig
from ..strategies.base import StrategySignal
from ..utils.logging import get_logger
from ..utils.numeric import safe_div

log = get_logger(__name__)

#: Verdict labels. These are the exact strings the operator log line carries.
ELIGIBLE = "ELIGIBLE"
SHADOW_ONLY = "SHADOW_ONLY"

#: The two named profiles, so a replay can compare them without hand-editing
#: config. STRICT demands a target of 3x round-trip costs (equivalently, costs
#: no more than a third of the target); BALANCED demands 2x (half). Both keep
#: every safety-relevant gate identical — net profit must still be positive,
#: net reward:risk must still clear 1.20, and the stop floor is the sizer's.
STRICT_PROFILE: dict[str, float] = {
    "min_target_to_cost_multiple": 3.0,
    "max_cost_pct_of_target": 1.0 / 3.0,
    "min_net_reward_risk": 1.20,
}
BALANCED_PROFILE: dict[str, float] = {
    "min_target_to_cost_multiple": 2.0,
    "max_cost_pct_of_target": 0.50,
    "min_net_reward_risk": 1.20,
}


@dataclass(frozen=True, slots=True)
class TradeCosts:
    """What a round trip costs, as fractions of notional.

    ``taker_fee_rate`` is the **discovered** account rate wherever possible —
    OKX's demo schedule differs from the config default, and a cost model built
    on an assumed fee is worse than useless because it is confidently wrong.
    ``source`` records which it was, so the dashboard can say so.
    """

    taker_fee_rate: float
    spread_bps: float
    slippage_bps: float
    source: str = "config"

    @property
    def verified(self) -> bool:
        """Whether the taker rate came from the exchange rather than a guess."""
        return self.source == "exchange"

    @property
    def fee_cost(self) -> float:
        """Two taker legs: the entry, and the protective stop or target."""
        return 2.0 * self.taker_fee_rate

    @property
    def spread_cost(self) -> float:
        """Half the spread on entry plus half on exit — one full spread."""
        return self.spread_bps / 10_000.0

    @property
    def slippage_cost(self) -> float:
        return 2.0 * self.slippage_bps / 10_000.0

    @property
    def round_trip(self) -> float:
        """Total cost of opening and closing, as a fraction of notional."""
        return self.fee_cost + self.spread_cost + self.slippage_cost

    def describe(self) -> str:
        return (
            f"{self.round_trip * 100:.4f}% round trip "
            f"(fees {self.fee_cost * 100:.4f}% + spread {self.spread_cost * 100:.4f}% "
            f"+ slippage {self.slippage_cost * 100:.4f}%, taker rate from {self.source})"
        )


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    """The measured answer for one setup, with every number that produced it."""

    eligible: bool
    strategy_id: str
    timeframe: str
    reason: str
    stop_distance_pct: float = 0.0
    target_distance_pct: float = 0.0
    cost_pct: float = 0.0
    expected_net_profit_pct: float = 0.0
    net_reward_risk: float = 0.0
    target_to_cost: float = 0.0
    spread_bps: float = 0.0
    cost_share_of_target: float = 0.0
    cost_detail: str = ""

    @property
    def label(self) -> str:
        return ELIGIBLE if self.eligible else SHADOW_ONLY

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "label": self.label,
            "strategy_id": self.strategy_id,
            "timeframe": self.timeframe,
            "reason": self.reason,
            "stop_distance_pct": round(self.stop_distance_pct * 100, 4),
            "target_distance_pct": round(self.target_distance_pct * 100, 4),
            "cost_pct": round(self.cost_pct * 100, 4),
            "expected_net_profit_pct": round(self.expected_net_profit_pct * 100, 4),
            "net_reward_risk": round(self.net_reward_risk, 3),
            "target_to_cost": round(self.target_to_cost, 3),
            "cost_pct_of_target": round(self.cost_share_of_target * 100, 1),
            "spread_bps": round(self.spread_bps, 2),
        }


@dataclass(slots=True)
class EligibilityStats:
    """Counts for the dashboard. A rejection here is *not* a blocked order."""

    assessed: int = 0
    eligible: int = 0
    shadow_only: int = 0
    by_reason: dict[str, int] | None = None

    def record(self, verdict: EligibilityVerdict) -> None:
        self.assessed += 1
        if verdict.eligible:
            self.eligible += 1
            return
        self.shadow_only += 1
        if self.by_reason is None:
            self.by_reason = {}
        key = verdict.reason.split(" —")[0].split(":")[0].strip()
        self.by_reason[key] = self.by_reason.get(key, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "assessed": self.assessed,
            "eligible": self.eligible,
            "shadow_only": self.shadow_only,
            "top_reasons": sorted(
                (self.by_reason or {}).items(), key=lambda kv: kv[1], reverse=True
            )[:5],
        }


class ActualTradeEligibility:
    """Decides whether a setup may be offered to the allocator at all.

    Every check answers "will this setup still be profitable after what it
    costs to trade it?". The thresholds come from configuration; the costs come
    from the exchange wherever it will tell us.
    """

    __slots__ = ("config", "risk", "stats")

    def __init__(self, config: ActualEligibilityConfig, risk: RiskConfig) -> None:
        self.config = config
        # The stop-distance floor is *shared* with the position sizer rather
        # than duplicated. Two independently-configured minimums would drift,
        # and the pre-filter would start passing setups the sizer refuses —
        # exactly the failure being fixed.
        self.risk = risk
        self.stats = EligibilityStats()

    # --- assessment -------------------------------------------------------

    def assess(
        self,
        signal: StrategySignal,
        *,
        costs: TradeCosts,
        orderbook_valid: bool = True,
    ) -> EligibilityVerdict:
        """Measure one setup against the costs it will actually pay."""
        stop_pct = signal.stop_distance_pct
        target_pct = (
            safe_div(abs(signal.target_price - signal.entry_reference), signal.entry_reference)
            if signal.target_price
            else 0.0
        )
        cost_pct = costs.round_trip
        # Costs are paid on the way in and on the way out, so they *reduce* the
        # reward and *increase* the realised loss. Both legs, not one.
        net_profit = target_pct - cost_pct
        net_risk = stop_pct + cost_pct
        net_rr = safe_div(net_profit, net_risk)
        target_to_cost = safe_div(target_pct, cost_pct)

        def verdict(eligible: bool, reason: str) -> EligibilityVerdict:
            result = EligibilityVerdict(
                eligible=eligible,
                strategy_id=signal.strategy_id,
                timeframe=signal.timeframe,
                reason=reason,
                stop_distance_pct=stop_pct,
                target_distance_pct=target_pct,
                cost_pct=cost_pct,
                expected_net_profit_pct=net_profit,
                net_reward_risk=net_rr,
                target_to_cost=target_to_cost,
                spread_bps=costs.spread_bps,
                cost_share_of_target=safe_div(cost_pct, target_pct),
                cost_detail=costs.describe(),
            )
            self.stats.record(result)
            self._log(result)
            return result

        # --- 0. the cost model must be real ------------------------------
        # Everything below is arithmetic on the fee rate. If that rate is a
        # configured guess rather than the account's actual schedule, every
        # verdict below is a guess too — and at OKX Demo the guess is five
        # times too cheap, which is exactly the direction that approves losing
        # trades. Refuse rather than pretend.
        if self.config.block_when_fee_rate_unverified and not costs.verified:
            return verdict(
                False,
                "the account fee schedule has not been verified with the exchange "
                f"(using {costs.source}) — refusing to price an actual trade from an "
                "assumed fee rate",
            )

        # --- 1. minimum stop distance (the sizer's own floor) ------------
        if stop_pct < self.risk.min_stop_distance_pct:
            return verdict(
                False,
                f"stop distance {stop_pct * 100:.4f}% is below the minimum "
                f"{self.risk.min_stop_distance_pct * 100:.4f}% — position sizing would "
                "refuse this setup",
            )
        if stop_pct > self.risk.max_stop_distance_pct:
            return verdict(
                False,
                f"stop distance {stop_pct * 100:.3f}% exceeds the maximum "
                f"{self.risk.max_stop_distance_pct * 100:.3f}%",
            )

        # --- 2. a target is required, and it must be worth reaching ------
        if signal.target_price is None or target_pct <= 0:
            return verdict(
                False,
                "no take-profit target — an actual trade needs a target to price "
                "its expected profit against costs",
            )
        if target_pct < self.config.min_target_distance_pct:
            return verdict(
                False,
                f"target distance {target_pct * 100:.4f}% is below the minimum "
                f"{self.config.min_target_distance_pct * 100:.4f}%",
            )

        # --- 3. liquidity ------------------------------------------------
        if self.config.require_orderbook and not orderbook_valid:
            return verdict(
                False, "order book is not valid — liquidity cannot be assessed"
            )
        if costs.spread_bps > self.config.max_spread_bps:
            return verdict(
                False,
                f"spread {costs.spread_bps:.2f} bps exceeds the maximum "
                f"{self.config.max_spread_bps:.2f} bps — insufficient liquidity",
            )

        # --- 4. the target must clear the cost of trading it -------------
        if net_profit <= 0:
            return verdict(
                False,
                f"expected net profit {net_profit * 100:+.4f}% is not positive — the "
                f"target move {target_pct * 100:.4f}% is smaller than round-trip costs "
                f"{cost_pct * 100:.4f}%",
            )
        cost_share = safe_div(cost_pct, target_pct)
        if target_to_cost < self.config.min_target_to_cost_multiple:
            return verdict(
                False,
                f"target is only {target_to_cost:.2f}× round-trip costs, below the "
                f"minimum {self.config.min_target_to_cost_multiple:.2f}× "
                f"(costs are {cost_share * 100:.0f}% of target profit)",
            )
        # The same constraint stated the other way round. Both are checked so a
        # future edit to one cannot quietly widen the other; the config
        # validator keeps them in agreement.
        if cost_share > self.config.max_cost_pct_of_target:
            return verdict(
                False,
                f"costs are {cost_share * 100:.0f}% of target profit, above the "
                f"maximum {self.config.max_cost_pct_of_target * 100:.0f}%",
            )

        # --- 5. net reward:risk, after costs on both legs ----------------
        if net_rr < self.config.min_net_reward_risk:
            return verdict(
                False,
                f"net reward:risk {net_rr:.2f} is below the minimum "
                f"{self.config.min_net_reward_risk:.2f} once costs are charged to both "
                "legs",
            )

        return verdict(
            True,
            f"net profit {net_profit * 100:+.4f}% at {net_rr:.2f} net R:R, target "
            f"{target_to_cost:.2f}× costs",
        )

    def filter(
        self,
        candidates: list[tuple[str, StrategySignal]],
        *,
        costs: TradeCosts,
        orderbook_valid: bool = True,
    ) -> tuple[list[tuple[str, StrategySignal]], list[EligibilityVerdict]]:
        """Split candidates into actual-eligible and shadow-only.

        The allocator only ever sees the first list, so exploration cannot draw
        an ineligible candidate — there is nothing ineligible in the pool.
        """
        eligible: list[tuple[str, StrategySignal]] = []
        verdicts: list[EligibilityVerdict] = []
        for strategy_id, signal in candidates:
            verdict = self.assess(signal, costs=costs, orderbook_valid=orderbook_valid)
            verdicts.append(verdict)
            if verdict.eligible:
                eligible.append((strategy_id, signal))
        return eligible, verdicts

    # --- operator log -----------------------------------------------------

    @staticmethod
    def _log(verdict: EligibilityVerdict) -> None:
        """The mandated `[ACTUAL ELIGIBILITY]` block, one line per fact."""
        level = log.info if verdict.eligible else log.debug
        level("ACTUAL ELIGIBILITY", f"strategy {verdict.strategy_id} ({verdict.timeframe}m)")
        level("ACTUAL ELIGIBILITY", f"stop distance {verdict.stop_distance_pct * 100:.4f}%")
        level("ACTUAL ELIGIBILITY", f"target distance {verdict.target_distance_pct * 100:.4f}%")
        level("ACTUAL ELIGIBILITY", f"estimated cost {verdict.cost_detail}")
        level(
            "ACTUAL ELIGIBILITY",
            f"expected net profit {verdict.expected_net_profit_pct * 100:+.4f}% "
            f"(net R:R {verdict.net_reward_risk:.2f})",
        )
        level("ACTUAL ELIGIBILITY", verdict.label)
        level("ACTUAL ELIGIBILITY", f"reason {verdict.reason}")
