"""Paper broker — fills orders the way a Solana memecoin DEX actually would.

Backtests that assume perfect fills are fiction on microcaps. We model:
  * DEX + platform fee (~1%: pump.fun 1%, Raydium 0.25% + priority fees)
  * price impact against pool liquidity (constant-product approximation:
    impact ≈ trade_size / (liquidity_side + trade_size))
  * a base slippage floor for latency (you never get the quoted price)

Selling into a rug (liquidity ~gone) recovers almost nothing — exactly
like real life.
"""
from __future__ import annotations

from ..models import TokenSnapshot

FEE_RATE = 0.011          # swap fee + priority fee, round trip charged per side
BASE_SLIPPAGE = 0.005     # latency floor per side
FIXED_FEE_USD = 0.05      # network + priority fee per transaction — this is
                          # what makes sub-$2 trades uneconomical in real life


class PaperBroker:
    def buy(self, t: TokenSnapshot, usd: float) -> tuple[float, float]:
        """Returns (tokens_received, usd_actually_spent)."""
        if t.price <= 0 or usd <= FIXED_FEE_USD:
            return 0.0, 0.0
        pool_side = max(1.0, t.liquidity / 2.0)
        impact = usd / (pool_side + usd)
        eff_price = t.price * (1.0 + BASE_SLIPPAGE + impact)
        usd_after_fee = (usd - FIXED_FEE_USD) * (1.0 - FEE_RATE)
        return usd_after_fee / eff_price, usd

    def sell(self, t: TokenSnapshot, tokens: float) -> float:
        """Returns USD received."""
        if t.price <= 0 or tokens <= 0:
            return 0.0
        gross = tokens * t.price
        pool_side = max(1.0, t.liquidity / 2.0)
        impact = gross / (pool_side + gross)
        eff_price = t.price * (1.0 - BASE_SLIPPAGE - impact)
        return max(0.0, tokens * eff_price * (1.0 - FEE_RATE) - FIXED_FEE_USD)
