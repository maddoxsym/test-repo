"""Gold paper broker — spot-dealer economics, no leverage, no interest.

Spot gold at a retail dealer/Islamic (swap-free) account: a spread of
roughly 0.03-0.06% round trip and no other costs. No overnight financing
exists in this model by design (swap-free), and buys are cash-capped —
the account can never hold more gold than it paid for in full.
"""
from __future__ import annotations

from memetrader.models import TokenSnapshot

HALF_SPREAD = 0.00022     # ~0.044% round trip


class GoldBroker:
    def buy(self, t: TokenSnapshot, usd: float) -> tuple[float, float]:
        if t.price <= 0 or usd <= 0:
            return 0.0, 0.0
        eff = t.price * (1.0 + HALF_SPREAD)
        return usd / eff, usd          # ounces received, cash spent (in full)

    def sell(self, t: TokenSnapshot, oz: float) -> float:
        if t.price <= 0 or oz <= 0:
            return 0.0
        return oz * t.price * (1.0 - HALF_SPREAD)
