"""Gold market simulator — minute bars, calibrated to the 2026 regime.

Calibration targets (July 2026, live research — see docs/GOLD_MARKET_STUDY.md):
  * spot ~$4,100 after the Jan-2026 $5,590 all-time high and correction
  * annualized volatility ~24% (2026 runs hot: >50% at the peak, ~30%
    now, vs a 20-year average of 17%) -> per-minute sigma ~4e-4
  * session structure: quiet Asia, active London, most active NY overlap
  * ~35% of days trend (macro flows), the rest mean-revert in a range
  * news jumps (CPI/NFP/FOMC): ~2/day, 0.1-0.6% moves
"""
from __future__ import annotations

import math
import random

from memetrader.models import TokenSnapshot, TokenSource

TICK_SECONDS = 60.0


def gold_snapshot(price: float, ts: float, price_high: float,
                  last_minute_prices: list[float]) -> TokenSnapshot:
    """Dress spot gold in the engine's snapshot interface. The 'safety'
    fields are all trivially clean — there is no rug in bullion."""
    vol_5m = sum(abs(last_minute_prices[i] - last_minute_prices[i - 1])
                 for i in range(1, len(last_minute_prices))) * 1000 \
        if len(last_minute_prices) > 1 else 0.0
    return TokenSnapshot(
        address="XAUUSD", symbol="XAU", name="Gold Spot (oz)",
        source=TokenSource.SIM, created_at=0.0,
        price=price, market_cap=1e13, liquidity=1e12,
        volume_5m=vol_5m, volume_1h=vol_5m * 12,
        holders=10**9, top10_holder_pct=0.0, deployer_holds_pct=0.0,
        mint_revoked=True, freeze_revoked=True, lp_locked_or_burned=True,
        has_socials=True, bonding_progress=1.0, graduated=True,
        smart_wallet_buys_30m=0, price_high=price_high, ts=ts,
    )


class GoldSim:
    """Evolves a spot-gold price path one minute at a time."""

    def __init__(self, seed: int = 7, start_price: float = 4100.0):
        self.rng = random.Random(seed)
        self.price = start_price
        self.tick = 0
        self.price_high = start_price
        self.day_anchor = start_price
        self.trending_day = False
        self.day_drift = 0.0
        self._roll_day()

    def _roll_day(self) -> None:
        rng = self.rng
        self.trending_day = rng.random() < 0.35
        self.day_drift = rng.choice([-1, 1]) * rng.uniform(1e-5, 4e-5) \
            if self.trending_day else 0.0
        self.day_anchor = self.price

    @property
    def now_ts(self) -> float:
        return 1_700_000_000.0 + self.tick * TICK_SECONDS

    def _session_mult(self) -> float:
        minute_of_day = self.tick % 1440
        h = minute_of_day / 60.0
        if 0 <= h < 7:      # Asia
            return 0.6
        if 7 <= h < 12:     # London
            return 1.2
        if 12 <= h < 16:    # London/NY overlap
            return 1.6
        if 16 <= h < 21:    # NY
            return 1.2
        return 0.7

    def step(self) -> float:
        rng = self.rng
        self.tick += 1
        if self.tick % 1440 == 0:
            self._roll_day()

        sigma = 4.0e-4 * self._session_mult()
        ret = rng.gauss(self.day_drift, sigma)
        if not self.trending_day:
            # gentle pull back toward the day's anchor (ranging behavior)
            ret += -0.002 * (self.price / self.day_anchor - 1.0) / 60.0
        # news jumps (CPI/NFP/FOMC prints): ~2/day in the 2026 regime
        if rng.random() < 2.0 / 1440:
            ret += rng.choice([-1, 1]) * rng.uniform(0.001, 0.006)

        self.price *= math.exp(ret)
        self.price_high = max(self.price_high, self.price)
        return self.price
