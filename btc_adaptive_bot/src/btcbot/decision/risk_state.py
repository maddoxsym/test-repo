"""System risk states: NORMAL → REDUCED → DEFENSIVE → PAUSED.

One label summarising how aggressively the system may act right now, derived
from measured conditions — drawdown, daily loss, weekly loss, and the
circuit-breaker state. The label feeds the decision engine (entry gating), the
leverage engine (caps), and the dashboard.

Every figure here is measured against **research equity**, never against the
OKX account total. The demo account may hold unrelated BTC, ETH or OKB whose
price moves would otherwise silently widen or shrink the loss limits; research
equity excludes all of it (see ``execution/research_equity.py``).

State semantics:

* **NORMAL** — full configured behaviour.
* **REDUCED** — early drawdown; sizes and leverage shrink (the sizing and
  leverage engines read the state), and low-conviction entries are skipped.
* **DEFENSIVE** — drawdown at the de-risk threshold; only high-conviction
  entries pass, leverage is capped hard.
* **PAUSED** — SAFE_MODE is active, or the daily or weekly loss limit is hit;
  no new entries at all. Existing positions continue to be managed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.schema import RiskConfig
from ..safety.circuit_breakers import CircuitBreakers
from ..utils.logging import get_logger
from ..utils.numeric import safe_div

log = get_logger(__name__)

NORMAL = "NORMAL"
REDUCED = "REDUCED"
DEFENSIVE = "DEFENSIVE"
PAUSED = "PAUSED"


@dataclass(slots=True)
class RiskStateSnapshot:
    state: str
    reason: str
    drawdown_pct: float
    daily_loss_pct: float
    weekly_loss_pct: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "drawdown_pct": round(self.drawdown_pct * 100, 2),
            "daily_loss_pct": round(self.daily_loss_pct * 100, 2),
            "weekly_loss_pct": round(self.weekly_loss_pct * 100, 2),
        }


class RiskStateTracker:
    """Derives the current risk state from measured account conditions."""

    def __init__(self, config: RiskConfig, breakers: CircuitBreakers) -> None:
        self.config = config
        self.breakers = breakers
        self._day_start_equity: float | None = None
        self._week_start_equity: float | None = None
        self._current = RiskStateSnapshot(NORMAL, "startup", 0.0, 0.0, 0.0)

    @property
    def current(self) -> RiskStateSnapshot:
        return self._current

    def start_of_day(self, equity: float) -> None:
        """Record the UTC-day opening **research** equity for the daily limit."""
        self._day_start_equity = equity
        if self._week_start_equity is None:
            self._week_start_equity = equity

    def start_of_week(self, equity: float) -> None:
        """Record the opening **research** equity for the weekly limit."""
        self._week_start_equity = equity

    @property
    def day_start_equity(self) -> float | None:
        return self._day_start_equity

    @property
    def week_start_equity(self) -> float | None:
        return self._week_start_equity

    def evaluate(self, *, equity: float, peak_equity: float) -> RiskStateSnapshot:
        drawdown = safe_div(max(0.0, peak_equity - equity), peak_equity)
        if self._day_start_equity is None:
            self._day_start_equity = equity
        if self._week_start_equity is None:
            self._week_start_equity = equity
        daily_loss = safe_div(
            max(0.0, self._day_start_equity - equity), self._day_start_equity
        )
        weekly_loss = safe_div(
            max(0.0, self._week_start_equity - equity), self._week_start_equity
        )

        if self.breakers.safe_mode.active:
            snapshot = RiskStateSnapshot(
                PAUSED, f"SAFE_MODE: {self.breakers.safe_mode.reason}",
                drawdown, daily_loss, weekly_loss,
            )
        elif daily_loss >= self.config.daily_loss_limit_pct:
            snapshot = RiskStateSnapshot(
                PAUSED,
                f"daily loss {daily_loss * 100:.1f}% of research equity ≥ limit "
                f"{self.config.daily_loss_limit_pct * 100:.1f}%",
                drawdown,
                daily_loss,
                weekly_loss,
            )
        elif weekly_loss >= self.config.weekly_loss_limit_pct:
            snapshot = RiskStateSnapshot(
                PAUSED,
                f"weekly loss {weekly_loss * 100:.1f}% of research equity ≥ limit "
                f"{self.config.weekly_loss_limit_pct * 100:.1f}%",
                drawdown,
                daily_loss,
                weekly_loss,
            )
        elif drawdown >= self.config.drawdown_derisk_threshold_pct:
            snapshot = RiskStateSnapshot(
                DEFENSIVE,
                f"drawdown {drawdown * 100:.1f}% ≥ de-risk threshold",
                drawdown,
                daily_loss,
                weekly_loss,
            )
        elif drawdown >= self.config.drawdown_derisk_threshold_pct / 2:
            snapshot = RiskStateSnapshot(
                REDUCED, f"drawdown {drawdown * 100:.1f}% (early de-risk)",
                drawdown, daily_loss, weekly_loss,
            )
        else:
            snapshot = RiskStateSnapshot(
                NORMAL, "conditions normal", drawdown, daily_loss, weekly_loss
            )

        if snapshot.state != self._current.state:
            log.info("RISK", f"Risk state {self._current.state} → {snapshot.state}: {snapshot.reason}")
        self._current = snapshot
        return snapshot
