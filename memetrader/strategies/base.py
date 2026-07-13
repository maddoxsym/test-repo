"""Strategy interface. A strategy looks at a token snapshot plus agent
context and either emits a buy Signal or stays silent. Exits are owned by
the risk engine, not strategies — discipline is centralized on purpose."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import StrategyParams
from ..models import SafetyReport, Signal, TokenSnapshot


@dataclass
class Context:
    """What the cooperating agents currently believe."""
    safety: SafetyReport
    narrative_delta: float      # from NewsAgent
    smart_money: float          # 0..1 from WhaleTracker


class Strategy:
    name = "base"

    def __init__(self, params: StrategyParams):
        self.params = params

    def evaluate(self, t: TokenSnapshot, ctx: Context) -> Signal | None:
        raise NotImplementedError

    def _signal(self, t: TokenSnapshot, confidence: float, reason: str) -> Signal:
        return Signal(
            strategy=self.name, address=t.address, symbol=t.symbol,
            side="buy", confidence=max(0.05, min(1.0, confidence)), reason=reason,
        )
