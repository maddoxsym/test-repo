"""
Spread quality gate.  Spread is measured in points (ticks of price — for
gold with tick size 0.01, 60 points = $0.60) and compared against the
configured maximum.  A spread above the limit vetoes new entries; the
regime detector independently flags ABNORMAL_SPREAD which disables all
entry models as well.
"""

from __future__ import annotations

from typing import Tuple

from ..core.config import Config


class SpreadFilter:

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def check(self, spread_points: float) -> Tuple[bool, str]:
        """(ok, reason).  spread_points <= 0 means 'unknown' and fails
        closed — never trade without a verifiable spread."""
        if spread_points <= 0:
            return False, "spread unknown/zero — cannot verify execution cost"
        if spread_points > self.cfg.max_spread_points:
            return False, (f"spread {spread_points:.0f} pts > max "
                           f"{self.cfg.max_spread_points:.0f} pts")
        return True, (f"spread {spread_points:.0f} pts <= max "
                      f"{self.cfg.max_spread_points:.0f} pts")
