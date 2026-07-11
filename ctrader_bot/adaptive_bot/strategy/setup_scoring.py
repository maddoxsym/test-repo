"""
Transparent 0-100 setup scoring with a full logged breakdown.

Scores are functions of market evidence only — never of recent P/L or
distance to the daily target.  The component weights are unchanged from the
legacy system so historical calibration (70 = B, 80 = A, 90 = A+) carries
over.  Note: with manual-only news protection the news component maxes at
3/5, so the practical maximum total is 98.

The M1 entry trigger and the spread check are hard GATES enforced by the
engine/order manager rather than score components: a setup without them is
rejected outright, which is stricter than any score bonus.
"""

from __future__ import annotations

from typing import Optional

from ..core.config import Config
from ..core.models import (Direction, FairValueGap, FVGState, OrderBlock,
                           ScoreBreakdown, SessionName, StructureEvent,
                           StructureEventKind, SweepEvent, TrendState, Zone)


class SetupScorer:

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def score(self,
              direction: Direction,
              htf_bias: TrendState,
              zone: Optional[Zone],
              sweep: Optional[SweepEvent],
              structure_event: Optional[StructureEvent],
              displacement: bool,
              fvg: Optional[FairValueGap],
              order_block: Optional[OrderBlock],
              pd_zone: str,                 # "PREMIUM"/"DISCOUNT"/"EQUILIBRIUM"
              session: SessionName,
              news_blocked: bool,
              news_protection_complete: bool,
              rr_tp1: float,
              target_is_liquidity: bool) -> ScoreBreakdown:
        b = ScoreBreakdown()

        # 15-minute bias alignment /15
        if (direction == Direction.LONG and htf_bias == TrendState.BULLISH) or \
           (direction == Direction.SHORT and htf_bias == TrendState.BEARISH):
            b.htf_alignment = 15.0
        elif htf_bias in (TrendState.RANGING, TrendState.UNDEFINED):
            b.htf_alignment = 8.0
        else:
            b.htf_alignment = 0.0     # countertrend

        # zone quality /15 (freshness, displacement, BOS link, overlaps)
        if zone is not None:
            b.zone_quality = round(15.0 * zone.quality(), 2)

        # liquidity sweep /15
        if sweep is not None and sweep.valid:
            pts = 9.0
            if sweep.closed_back:
                pts += 3.0
            if sweep.displaced_away:
                pts += 3.0
            b.liquidity_sweep = pts

        # 5-minute structure confirmation /15
        if structure_event is not None:
            b.structure_confirmation = {
                StructureEventKind.MSS: 15.0,
                StructureEventKind.CHOCH: 12.0,
                StructureEventKind.BOS: 11.0,
            }[structure_event.kind]

        # displacement /10
        if displacement or (structure_event and structure_event.displacement):
            b.displacement = 10.0

        # FVG / OB confluence /10
        conf = 0.0
        if fvg is not None and fvg.state != FVGState.MITIGATED:
            conf += 5.0
        if order_block is not None and not order_block.invalidated:
            conf += 5.0
        b.confluence = conf

        # premium/discount /5
        if (direction == Direction.LONG and pd_zone == "DISCOUNT") or \
           (direction == Direction.SHORT and pd_zone == "PREMIUM"):
            b.premium_discount = 5.0
        elif pd_zone == "EQUILIBRIUM":
            b.premium_discount = 2.0

        # session quality /5
        b.session_quality = {
            SessionName.OVERLAP: 5.0, SessionName.LONDON: 5.0,
            SessionName.NEW_YORK: 4.0, SessionName.ASIA: 2.0,
            SessionName.OFF_HOURS: 0.0}[session]

        # news safety /5 (manual-only protection can never claim the full 5)
        if news_blocked:
            b.news_safety = 0.0
        elif news_protection_complete:
            b.news_safety = 5.0
        else:
            b.news_safety = 3.0    # manual/schedule-based protection

        # target quality / net RR /5
        if rr_tp1 >= 3.0:
            b.target_quality = 5.0
        elif rr_tp1 >= self.cfg.preferred_rr:
            b.target_quality = 4.0
        elif rr_tp1 >= self.cfg.min_rr:
            b.target_quality = 2.0
        if target_is_liquidity and b.target_quality > 0:
            b.target_quality = min(5.0, b.target_quality + 1.0)
        return b
