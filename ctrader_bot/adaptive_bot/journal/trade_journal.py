"""
CSV journal: every accepted/rejected setup and every completed trade,
written best-effort to a local folder.

Default location: <home>/Documents/XAUUSD_Adaptive_Bot/
    setups_YYYY-MM.csv   one row per evaluated setup (accepted or rejected)
    trades_YYYY-MM.csv   one row per completed trade

File IO on macOS can be sandboxed depending on how cTrader was installed;
if a write fails the journal disables itself for the session with a single
log line and the bot keeps running on cTrader's own log alone.  No secrets
are ever written — only market/trade data.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Callable, List, Optional

from ..core.config import Config
from ..core.models import Setup, Trade

SETUP_FIELDS = [
    "time", "symbol", "decision", "rejection_reason", "model", "direction",
    "timeframes", "session", "regime", "htf_bias", "premium_discount",
    "zone", "liquidity_event", "m5_confirmation", "m1_trigger",
    "news_status", "spread_points", "score", "grade",
    "score_htf", "score_zone", "score_sweep", "score_structure",
    "score_displacement", "score_confluence", "score_pd", "score_session",
    "score_news", "score_target",
    "risk_pct", "volume_units", "entry", "stop_loss", "take_profit",
    "rr_tp1", "stop_reason", "target_reason",
]

TRADE_FIELDS = [
    "trade_id", "position_id", "direction", "entry_time", "exit_time",
    "entry_price", "exit_price", "stop_loss", "take_profit", "volume_units",
    "profit", "r_multiple", "mfe_r", "mae_r", "setup_score", "setup_model",
    "exit_reason",
]


class TradeJournal:

    def __init__(self, cfg: Config, log: Callable[[str], None]):
        self.cfg = cfg
        self.log = log
        self.enabled = cfg.journal_enabled
        self.directory: Optional[str] = None
        if self.enabled:
            self.directory = self._prepare_dir()
            if self.directory is None:
                self.enabled = False

    def _prepare_dir(self) -> Optional[str]:
        base = self.cfg.journal_dir.strip()
        candidates = []
        if base:
            candidates.append(base)
        home = os.path.expanduser("~")
        candidates.append(os.path.join(home, "Documents", "XAUUSD_Adaptive_Bot"))
        candidates.append(os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "..", "..", "journal_output"))
        for cand in candidates:
            try:
                os.makedirs(cand, exist_ok=True)
                probe = os.path.join(cand, ".write_probe")
                with open(probe, "w", encoding="utf-8") as fh:
                    fh.write("ok")
                os.remove(probe)
                return os.path.abspath(cand)
            except OSError:
                continue
        self.log("JOURNAL: no writable directory found — CSV journal "
                 "disabled for this session (cTrader log still records "
                 "everything)")
        return None

    def _append(self, filename: str, fields: List[str], row: dict) -> None:
        if not self.enabled or self.directory is None:
            return
        path = os.path.join(self.directory, filename)
        try:
            fresh = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields,
                                        extrasaction="ignore")
                if fresh:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:
            self.enabled = False
            self.log(f"JOURNAL: write failed ({exc}) — CSV journal disabled "
                     f"for this session")

    # ------------------------------------------------------------------ setups
    def record_setup(self, now: datetime, symbol: str, setup: Optional[Setup],
                     accepted: bool, rejection_reason: str = "",
                     spread_points: float = 0.0, session: str = "",
                     news_status: str = "", risk_pct: float = 0.0,
                     volume_units: float = 0.0, m1_trigger: str = "",
                     model: str = "", direction: str = "",
                     regime: str = "", htf_bias: str = "",
                     pd_zone: str = "") -> None:
        row = {
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "decision": "ACCEPTED" if accepted else "REJECTED",
            "rejection_reason": rejection_reason,
            "timeframes": "M15/M5/M1",
            "session": session,
            "news_status": news_status,
            "spread_points": f"{spread_points:.1f}",
            "risk_pct": f"{risk_pct:.4%}" if risk_pct else "",
            "volume_units": volume_units or "",
            "m1_trigger": m1_trigger,
            "model": model, "direction": direction,
            "regime": regime, "htf_bias": htf_bias,
            "premium_discount": pd_zone,
        }
        if setup is not None:
            b = setup.breakdown
            row.update({
                "model": setup.model.value,
                "direction": setup.direction.value,
                "regime": setup.regime.value,
                "htf_bias": setup.htf_bias.value,
                "zone": (f"{setup.zone.kind.value} {setup.zone.pattern.value} "
                         f"{setup.zone.lower:.2f}-{setup.zone.upper:.2f} "
                         f"fresh {setup.zone.freshness:.2f}"
                         if setup.zone else ""),
                "liquidity_event": (f"sweep {setup.sweep.level.kind.value} @ "
                                    f"{setup.sweep.level.price:.2f}"
                                    if setup.sweep else ""),
                "m5_confirmation": (setup.structure_event.kind.value
                                    if setup.structure_event else ""),
                "m1_trigger": setup.m1_trigger or m1_trigger,
                "score": f"{setup.score:.1f}",
                "grade": setup.grade.value,
                "score_htf": b.htf_alignment, "score_zone": b.zone_quality,
                "score_sweep": b.liquidity_sweep,
                "score_structure": b.structure_confirmation,
                "score_displacement": b.displacement,
                "score_confluence": b.confluence,
                "score_pd": b.premium_discount,
                "score_session": b.session_quality,
                "score_news": b.news_safety,
                "score_target": b.target_quality,
                "entry": f"{setup.entry_price:.2f}",
                "stop_loss": f"{setup.stop_price:.2f}",
                "take_profit": f"{setup.tp1:.2f}",
                "rr_tp1": f"{setup.rr_to(setup.tp1):.2f}",
                "stop_reason": setup.stop_reason,
                "target_reason": setup.target_reason,
            })
        self._append(f"setups_{now:%Y-%m}.csv", SETUP_FIELDS, row)

    # ------------------------------------------------------------------ trades
    def record_trade(self, trade: Trade) -> None:
        row = {
            "trade_id": trade.trade_id,
            "position_id": trade.position_id,
            "direction": trade.direction.value,
            "entry_time": (trade.entry_time.strftime("%Y-%m-%d %H:%M:%S")
                           if trade.entry_time else ""),
            "exit_time": (trade.exit_time.strftime("%Y-%m-%d %H:%M:%S")
                          if trade.exit_time else ""),
            "entry_price": f"{trade.entry_price:.2f}",
            "exit_price": f"{trade.exit_price:.2f}",
            "stop_loss": f"{trade.stop_price:.2f}",
            "take_profit": f"{trade.tp1:.2f}",
            "volume_units": trade.initial_volume_units,
            "profit": f"{trade.profit:.2f}",
            "r_multiple": f"{trade.r_multiple():.2f}",
            "mfe_r": f"{trade.mfe:.2f}",
            "mae_r": f"{trade.mae:.2f}",
            "setup_score": f"{trade.setup.score:.1f}",
            "setup_model": trade.setup.model.value,
            "exit_reason": trade.exit_reason.value if trade.exit_reason else "",
        }
        when = trade.exit_time or trade.entry_time or datetime.now()
        self._append(f"trades_{when:%Y-%m}.csv", TRADE_FIELDS, row)
