"""
The real-position record and the bid/ask conversion for broker orders.

Two details matter here and both were sources of error in V4.

1. BROKER LEVEL CONVERSION.  Every level the strategy produces is a BID
   level.  cTrader triggers a LONG's stop/target against the bid, but a
   SHORT's against the ask.  So a short's orders must be placed one spread
   above the bid level it means:

        long   SL/TP submitted as-is
        short  SL/TP submitted at level + spread

   Without this a short's stop sits one spread too tight and its target one
   spread too far, which is exactly the asymmetry that made V4's shorts look
   better than they were.

2. PARTIAL CLOSES PRODUCE SEVERAL HISTORY ENTRIES.  cTrader writes one
   HistoricalTrade per closing deal, all sharing the PositionId.  Reading only
   the first one loses the partial's profit, so settlement sums every entry
   for the position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from adaptive_bot.core.models import Direction
from .management import ManagedTrade


def broker_level(level: float, direction: Direction,
                 spread_price: float) -> float:
    """Convert a BID level into the level to submit to the broker."""
    if direction == Direction.LONG:
        return level
    return level + spread_price


def bid_level(broker_price: float, direction: Direction,
              spread_price: float) -> float:
    """Inverse of broker_level, for reading levels back off a position."""
    if direction == Direction.LONG:
        return broker_price
    return broker_price - spread_price


@dataclass
class RealTradeRecord:
    """One real DEMO position, with the same management state as a shadow."""
    trade_id: str
    position_id: int
    sid: str
    family: str
    version: int
    direction: Direction
    managed: ManagedTrade
    entry_time: datetime
    risk_pct: float
    risk_money_sized: float
    confluence_score: float
    confluence_detail: str
    stop_reason: str
    tp1_reason: str
    target_reason: str
    sweep_kind: str
    regime: str
    session: str
    htf_bias: str
    location: str
    spread_points: float
    params: Dict[str, float] = field(default_factory=dict)
    adopted: bool = False
    # settlement
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_label: str = ""
    gross_money: float = 0.0
    commission_money: float = 0.0
    net_money: float = 0.0
    gross_r: float = 0.0
    net_r: float = 0.0

    # ------------------------------------------------------------- state io
    def snapshot(self) -> dict:
        m = self.managed
        return {
            "trade_id": self.trade_id, "position_id": self.position_id,
            "sid": self.sid, "family": self.family, "version": self.version,
            "direction": self.direction.value,
            "entry_time": self.entry_time.isoformat(),
            "risk_pct": self.risk_pct,
            "risk_money_sized": self.risk_money_sized,
            "confluence_score": self.confluence_score,
            "confluence_detail": self.confluence_detail,
            "stop_reason": self.stop_reason, "tp1_reason": self.tp1_reason,
            "target_reason": self.target_reason, "sweep_kind": self.sweep_kind,
            "regime": self.regime, "session": self.session,
            "htf_bias": self.htf_bias, "location": self.location,
            "spread_points": self.spread_points, "params": dict(self.params),
            "adopted": self.adopted,
            "managed": {
                "trade_id": m.trade_id, "sid": m.sid, "family": m.family,
                "direction": m.direction.value, "entry": m.entry,
                "initial_stop": m.initial_stop, "stop": m.stop, "tp1": m.tp1,
                "target": m.target, "units_initial": m.units_initial,
                "units": m.units, "risk_dist": m.risk_dist,
                "risk_money": m.risk_money,
                "entry_time": m.entry_time.isoformat() if m.entry_time else "",
                "partial_done": m.partial_done,
                "partial_skipped": m.partial_skipped,
                "partial_units": m.partial_units,
                "partial_price": m.partial_price,
                "partial_reason": m.partial_reason,
                "be_done": m.be_done, "be_reason": m.be_reason,
                "trail_active": m.trail_active,
                "trail_updates": list(m.trail_updates),
                "last_trail_swing": m.last_trail_swing,
                "early_exit_reason": m.early_exit_reason,
                "early_exit_partial_done": m.early_exit_partial_done,
                "bars_open": m.bars_open, "mfe_r": m.mfe_r, "mae_r": m.mae_r,
                "meta": dict(m.meta)}}

    @staticmethod
    def from_dict(d: dict) -> Optional["RealTradeRecord"]:
        try:
            md = d["managed"]
            direction = Direction(d["direction"])
            entry_time_raw = md.get("entry_time", "")
            managed = ManagedTrade(
                trade_id=md["trade_id"], sid=md["sid"], family=md["family"],
                direction=Direction(md["direction"]), entry=float(md["entry"]),
                initial_stop=float(md["initial_stop"]),
                stop=float(md["stop"]), tp1=float(md["tp1"]),
                target=float(md["target"]),
                units_initial=float(md["units_initial"]),
                units=float(md["units"]), risk_dist=float(md["risk_dist"]),
                risk_money=float(md["risk_money"]),
                entry_time=datetime.fromisoformat(entry_time_raw)
                if entry_time_raw else None,
                partial_done=bool(md.get("partial_done", False)),
                partial_skipped=bool(md.get("partial_skipped", False)),
                partial_units=float(md.get("partial_units", 0.0)),
                partial_price=float(md.get("partial_price", 0.0)),
                partial_reason=md.get("partial_reason", ""),
                be_done=bool(md.get("be_done", False)),
                be_reason=md.get("be_reason", ""),
                trail_active=bool(md.get("trail_active", False)),
                trail_updates=list(md.get("trail_updates", [])),
                last_trail_swing=md.get("last_trail_swing", ""),
                early_exit_reason=md.get("early_exit_reason", ""),
                early_exit_partial_done=bool(
                    md.get("early_exit_partial_done", False)),
                bars_open=int(md.get("bars_open", 0)),
                mfe_r=float(md.get("mfe_r", 0.0)),
                mae_r=float(md.get("mae_r", 0.0)),
                meta={str(k): float(v)
                      for k, v in dict(md.get("meta", {})).items()})
            return RealTradeRecord(
                trade_id=d["trade_id"], position_id=int(d["position_id"]),
                sid=d["sid"], family=d["family"],
                version=int(d.get("version", 1)), direction=direction,
                managed=managed,
                entry_time=datetime.fromisoformat(d["entry_time"]),
                risk_pct=float(d.get("risk_pct", 0.0)),
                risk_money_sized=float(d.get("risk_money_sized", 0.0)),
                confluence_score=float(d.get("confluence_score", 0.0)),
                confluence_detail=d.get("confluence_detail", ""),
                stop_reason=d.get("stop_reason", ""),
                tp1_reason=d.get("tp1_reason", ""),
                target_reason=d.get("target_reason", ""),
                sweep_kind=d.get("sweep_kind", ""),
                regime=d.get("regime", ""), session=d.get("session", ""),
                htf_bias=d.get("htf_bias", ""), location=d.get("location", ""),
                spread_points=float(d.get("spread_points", 0.0)),
                params={str(k): float(v)
                        for k, v in dict(d.get("params", {})).items()},
                adopted=bool(d.get("adopted", False)))
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ row
    def to_row(self, broker_profit: float) -> dict:
        m = self.managed
        return {
            "trade_id": self.trade_id, "position_id": self.position_id,
            "sid": self.sid, "family": self.family, "version": self.version,
            "direction": self.direction.value,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat() if self.exit_time else "",
            "entry": f"{m.entry:.2f}",
            "initial_stop": f"{m.initial_stop:.2f}",
            "final_stop": f"{m.stop:.2f}", "tp1": f"{m.tp1:.2f}",
            "target": f"{m.target:.2f}", "exit_price": f"{self.exit_price:.2f}",
            "exit_label": self.exit_label,
            "units_initial": f"{m.units_initial:g}",
            "units_partial": f"{m.partial_units:g}",
            "risk_pct": f"{self.risk_pct:.4%}",
            "risk_money": f"{m.risk_money:.2f}",
            "confluence_score": f"{self.confluence_score:.0f}",
            "confluence_detail": self.confluence_detail,
            "gross_money": f"{self.gross_money:.2f}",
            "commission": f"{self.commission_money:.2f}",
            "net_money": f"{self.net_money:.2f}",
            "gross_r": f"{self.gross_r:+.3f}", "net_r": f"{self.net_r:+.3f}",
            "mfe_r": f"{m.mfe_r:.3f}", "mae_r": f"{m.mae_r:.3f}",
            "bars_open": m.bars_open,
            "partial_taken": "yes" if m.partial_done else
                             ("skipped" if m.partial_skipped else "no"),
            "partial_price": f"{m.partial_price:.2f}" if m.partial_done else "",
            "partial_reason": m.partial_reason,
            "be_activated": "yes" if m.be_done else "no",
            "be_reason": m.be_reason,
            "trail_updates": " | ".join(m.trail_updates),
            "early_exit_reason": m.early_exit_reason,
            "stop_reason": self.stop_reason, "tp1_reason": self.tp1_reason,
            "target_reason": self.target_reason, "sweep_kind": self.sweep_kind,
            "regime": self.regime, "session": self.session,
            "htf_bias": self.htf_bias, "location": self.location,
            "spread_points": f"{self.spread_points:.0f}",
            "broker_profit": f"{broker_profit:.2f}"}


def shadow_row(t) -> dict:
    """CSV row for a shadow trade (VirtualTrade)."""
    m = t.managed
    return {
        "trade_id": t.trade_id, "sid": t.sid, "family": t.family,
        "version": t.version, "direction": t.direction.value,
        "signal_time": t.signal_time.isoformat(),
        "entry_time": t.entry_time.isoformat() if t.entry_time else "",
        "exit_time": t.exit_time.isoformat() if t.exit_time else "",
        "entry_ref": f"{t.entry_ref:.2f}", "entry": f"{t.entry:.2f}",
        "initial_stop": f"{m.initial_stop:.2f}" if m else "",
        "final_stop": f"{m.stop:.2f}" if m else "",
        "tp1": f"{t.planned_tp1:.2f}", "target": f"{t.planned_target:.2f}",
        "exit_price": f"{t.exit_price:.2f}", "exit_label": t.exit_label,
        "units_initial": f"{m.units_initial:g}" if m else "",
        "units_partial": f"{m.partial_units:g}" if m else "",
        "risk_pct": f"{t.risk_pct:.4%}",
        "risk_money": f"{m.risk_money:.2f}" if m else "",
        "confluence_score": f"{t.confluence_score:.0f}",
        "confluence_detail": t.confluence_detail,
        "gross_money": f"{t.gross_money:.2f}",
        "commission": f"{t.commission_money:.2f}",
        "net_money": f"{t.net_money:.2f}", "gross_r": f"{t.gross_r:+.3f}",
        "net_r": f"{t.net_r:+.3f}",
        "mfe_r": f"{t.mfe_r:.3f}", "mae_r": f"{t.mae_r:.3f}",
        "bars_open": t.bars_open,
        "partial_taken": ("yes" if (m and m.partial_done) else
                          ("skipped" if (m and m.partial_skipped) else "no")),
        "partial_price": f"{m.partial_price:.2f}" if (m and m.partial_done)
                         else "",
        "partial_reason": m.partial_reason if m else "",
        "be_activated": "yes" if (m and m.be_done) else "no",
        "be_reason": m.be_reason if m else "",
        "trail_updates": " | ".join(m.trail_updates) if m else "",
        "early_exit_reason": m.early_exit_reason if m else "",
        "stop_reason": t.stop_reason, "tp1_reason": t.tp1_reason,
        "target_reason": t.target_reason, "sweep_kind": t.sweep_kind,
        "regime": t.regime, "session": t.session, "htf_bias": t.htf_bias,
        "location": t.location,
        "spread_points": f"{t.spread_points_signal:.0f}",
        "status": t.status, "discard_reason": t.discard_reason,
        "suspect": "yes" if t.suspect else "no",
        "suspect_reason": t.suspect_reason}
